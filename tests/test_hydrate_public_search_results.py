from scripts.hydrate_public_search_results import build_hydrated_payload, hydration_attempts


def test_build_hydrated_payload_preserves_source_links_and_full_hydrated_body():
    original = {
        "trace_id": "search-trace-1",
        "source_type": "Search",
        "source_name": "public-search",
        "source_url": "https://target.example/thread/1",
        "search_query_url": "https://search.example?q=risk",
        "search_query": "risk",
        "query_theme": "blackgray",
        "query_term": "risk term",
        "query_term_stage": "variant",
        "query_variant_index": 3,
        "result_title": "Risk thread",
        "result_rank": 7,
        "matched_keywords": ["risk"],
    }
    hydrated_body = "full hydrated page body " + ("detail " * 120)
    hydrated_row = {
        "trace_id": "hydrated-trace-1",
        "source_type": "Hydrated",
        "source_name": "hydrated-source",
        "source_url": "https://mirror.example/thread/1",
        "content_text": hydrated_body,
        "raw_snippet": "hydrated snippet",
    }

    payload = build_hydrated_payload(
        original,
        hydrated_row,
        "http://r.jina.ai/http://target.example/thread/1",
    )

    assert payload["collection_stage"] == "hydrated_page"
    assert payload["capture_snapshot_uri"] == "http://r.jina.ai/http://target.example/thread/1"
    assert payload["raw_payload_uri"] == "http://r.jina.ai/http://target.example/thread/1"
    assert payload["hydrated_from_trace_id"] == "search-trace-1"
    assert payload["source_url"] == "https://target.example/thread/1"
    assert payload["search_query_url"] == "https://search.example?q=risk"
    assert payload["content_text"] == hydrated_body


def test_hydration_attempts_try_mirror_before_direct_target():
    attempts = hydration_attempts("https://target.example/thread/1")

    assert attempts[0]["snapshot_url"].startswith("http://r.jina.ai/http://target.example/thread/1")
    assert attempts[0]["allowed_domains"] == ("r.jina.ai",)
    assert attempts[1]["snapshot_url"] == "https://target.example/thread/1"
    assert attempts[1]["allowed_domains"] == ("target.example",)
