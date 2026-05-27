"""Collect black/gray raw tweets from X recent-search API."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector.relevance import DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS, decide_text_relevance
from src.config_loader import load_yaml_file, resolve_project_path
from storage.sql_backend import connect


API_URL = "https://api.x.com/2/tweets/search/recent"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect X recent-search results into BlackAgent raw_records.")
    parser.add_argument(
        "--config",
        default="config/x_watch.example.yaml",
        help="Project-relative YAML config path (default: config/x_watch.example.yaml)",
    )
    parser.add_argument("--db", default=None, help="Optional DB path override")
    parser.add_argument("--jsonl-path", default=None, help="Optional JSONL output path override")
    parser.add_argument("--fresh-state", action="store_true", help="Delete saved state before collecting")
    return parser.parse_args()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_raw_record(
    *,
    text: str,
    source_name: str,
    source_url: str,
    legal_basis: str,
    publish_time: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "hash_id": sha256(text.encode("utf-8")).hexdigest(),
        "trace_id": str(uuid4()),
        "source_type": "IM",
        "source_name": source_name,
        "source_url": source_url,
        "capture_snapshot_uri": "",
        "collector_version": "x_recent_search_v1",
        "raw_payload_uri": source_url,
        "legal_basis": legal_basis,
        "crawl_time": utc_now_iso(),
        "publish_time": publish_time or utc_now_iso(),
        "content_text": text,
    }
    if extra:
        payload.update(extra)
    return payload


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"since_id_by_query": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(payload, ensure_ascii=False) + "\n")


def query_key(query: str) -> str:
    return sha256(query.encode("utf-8")).hexdigest()[:16]


def request_recent_search(
    *,
    bearer_token: str,
    query: str,
    max_results: int,
    since_id: str | None,
) -> dict[str, Any]:
    params = {
        "query": query,
        "max_results": str(max_results),
        "tweet.fields": "created_at,author_id,conversation_id,lang,entities,public_metrics",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    if since_id:
        params["since_id"] = since_id
    url = API_URL + "?" + urllib_parse.urlencode(params)
    req = urllib_request.Request(
        url,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "BlackAgent-XRecentSearchCollector/0.1",
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib_request.urlopen(req, timeout=30) as response:  # noqa: S310 - explicit authorized API endpoint
        return json.loads(response.read().decode("utf-8", errors="replace"))


def main() -> int:
    args = parse_args()
    cfg = load_yaml_file(resolve_project_path(args.config))
    x_cfg = cfg.get("x") or {}
    bearer_token = str(x_cfg.get("bearer_token") or "").strip()
    if not bearer_token:
        raise SystemExit("x.bearer_token is required")

    queries = [str(item).strip() for item in x_cfg.get("watch", {}).get("queries", []) if str(item).strip()]
    if not queries:
        raise SystemExit("x.watch.queries must contain at least one query")

    db_path = resolve_project_path(args.db or x_cfg.get("db") or "data/blackagent_x.db")
    state_path = db_path.with_suffix(db_path.suffix + ".state.json")
    jsonl_path = args.jsonl_path or x_cfg.get("collection", {}).get("save_jsonl_path")
    jsonl_file = resolve_project_path(jsonl_path) if jsonl_path else None
    legal_basis = str(x_cfg.get("legal_basis") or "AUTHORIZED_PARTNER")
    source_name_prefix = str(x_cfg.get("source_name_prefix") or "x_recent_search")
    max_results = int(x_cfg.get("collection", {}).get("max_results_per_query", 20) or 20)
    include_keywords = x_cfg.get("collection", {}).get("include_keywords") or []
    exclude_keywords = x_cfg.get("collection", {}).get("exclude_keywords") or list(DEFAULT_DEFENSIVE_EXCLUDE_KEYWORDS)
    include_themes = x_cfg.get("collection", {}).get("include_themes") or []
    exclude_themes = x_cfg.get("collection", {}).get("exclude_themes") or []
    min_keyword_hits = int(x_cfg.get("collection", {}).get("min_keyword_hits", 1) or 1)

    if args.fresh_state and state_path.exists():
        state_path.unlink()
    state = load_state(state_path)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()

    persisted_count = 0
    query_summaries: list[dict[str, Any]] = []

    for query in queries:
        qkey = query_key(query)
        since_id = str(state.setdefault("since_id_by_query", {}).get(qkey) or "") or None
        payload = request_recent_search(
            bearer_token=bearer_token,
            query=query,
            max_results=max_results,
            since_id=since_id,
        )
        users = {
            str(user.get("id")): user
            for user in (payload.get("includes", {}) or {}).get("users", [])
            if isinstance(user, dict) and user.get("id") is not None
        }
        tweets = [item for item in payload.get("data", []) if isinstance(item, dict)]
        kept = 0
        max_seen_id: int | None = None

        for tweet in tweets:
            tweet_id = str(tweet.get("id") or "")
            if tweet_id.isdigit():
                max_seen_id = max(int(tweet_id), max_seen_id or 0)

            text = str(tweet.get("text") or "").strip()
            if not text:
                continue
            decision = decide_text_relevance(
                text,
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                include_themes=include_themes,
                exclude_themes=exclude_themes,
                min_keyword_hits=min_keyword_hits,
            )
            if not decision.relevant:
                continue

            author_id = str(tweet.get("author_id") or "")
            author = users.get(author_id) or {}
            author_username = str(author.get("username") or "").strip()
            source_url = (
                f"https://x.com/{author_username}/status/{tweet_id}"
                if author_username and tweet_id
                else f"https://x.com/i/web/status/{tweet_id}"
            )
            raw = build_raw_record(
                text=text,
                source_name=f"{source_name_prefix}:{qkey}",
                source_url=source_url,
                legal_basis=legal_basis,
                publish_time=tweet.get("created_at"),
                extra={
                    "post_id": tweet_id,
                    "author_id": author_id or None,
                    "author_username": author_username or None,
                    "conversation_id": tweet.get("conversation_id"),
                    "lang": tweet.get("lang"),
                    "query": query,
                    "public_metrics": tweet.get("public_metrics"),
                    "matched_keywords": list(decision.matched_keywords),
                    "excluded_keywords": list(decision.excluded_keywords),
                    "matched_themes": list(decision.matched_themes),
                    "excluded_themes": list(decision.excluded_themes),
                    "keyword_hit_count": decision.hit_count,
                    "relevance_version": decision.policy_version,
                },
            )
            backend.save_raw(raw)
            append_jsonl(jsonl_file, raw)
            persisted_count += 1
            kept += 1

        if max_seen_id is not None:
            state["since_id_by_query"][qkey] = str(max_seen_id)
        query_summaries.append(
            {
                "query": query,
                "fetched_count": len(tweets),
                "persisted_count": kept,
                "since_id": state["since_id_by_query"].get(qkey),
            }
        )

    save_state(state_path, state)
    backend.close()

    print(
        json.dumps(
            {
                "status": "completed",
                "db_path": str(db_path),
                "query_count": len(queries),
                "persisted_count": persisted_count,
                "queries": query_summaries,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
