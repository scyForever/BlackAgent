"""Hydrate direct public pages for search-discovered black/gray raw records."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.collector import HTTPFeedCollector, HTTPFeedConfig
from src.collector.base_collector import model_dump
from src.enhancement.engine import PhaseTwoThreeEngine
from storage.sql_backend import connect


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


TERMINAL_TAIL_RULE_VERSION = "tail_rule_v1"
SECOND_HAND_TAIL_SOURCES = {"second_hand_blackgray_mass_search", "carousell_blackgray_batch7_search"}
TIEBA_FORUM_TAIL_SOURCES = {"tieba_forum_blackgray_mass_search"}
SECURITY_VERIFICATION_MARKERS = (
    "performing security verification",
    "this website uses a security service",
    "just a moment",
    "verify you are not a bot",
)
AUTH_GATE_URL_MARKERS = ("/login", "/signup", "/register")
TIEBA_GENERIC_FORUM_MARKERS = ("吧-百度贴吧", "精品贴", "回复于")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch direct page snapshots for public search-result raw rows.")
    parser.add_argument("--db", default="data/blackagent_blackgray_all.db", help="SQLite DB path to read/write")
    parser.add_argument("--limit", type=int, default=0, help="Optional max result URLs to hydrate (0 = no limit)")
    parser.add_argument(
        "--source-names",
        default="",
        help="Optional comma-separated source_name allowlist; default hydrates all rows with search_query_url",
    )
    parser.add_argument(
        "--only-unhydrated",
        action="store_true",
        help="Only hydrate search-result URLs that do not already have a hydrated_page row for the same source_name",
    )
    parser.add_argument(
        "--query-term-stage",
        choices=("core", "variant"),
        default="",
        help="Optional filter to hydrate only rows from the given query_term_stage",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0, help="Read timeout per hydrated page request")
    parser.add_argument("--rate-limit-per-minute", type=int, default=12, help="Per-host request budget for r.jina.ai")
    parser.add_argument("--retry-attempts", type=int, default=2, help="Retry count for retryable HTTP errors")
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=5.0,
        help="Initial retry backoff seconds for retryable HTTP errors",
    )
    parser.add_argument(
        "--retry-backoff-multiplier",
        type=float,
        default=2.0,
        help="Exponential multiplier for retry backoff",
    )
    parser.add_argument(
        "--run-pipeline",
        action="store_true",
        help="Run the cleaned/classification/entity-extraction pipeline for newly hydrated rows",
    )
    return parser.parse_args()


def mirror_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    normalized_path = quote(unquote(parsed.path or ""), safe="/:@-._~!$&'()*+,;=")
    normalized_query = quote(unquote(parsed.query or ""), safe="=&/:?@-._~!$'()*+,;")
    normalized_fragment = quote(unquote(parsed.fragment or ""), safe="=&/:?@-._~!$'()*+,;")
    normalized_url = urlunsplit(
        (
            parsed.scheme or "https",
            parsed.netloc,
            normalized_path,
            normalized_query,
            normalized_fragment,
        )
    )
    return f"http://r.jina.ai/http://{normalized_url.removeprefix('https://').removeprefix('http://')}"


def hydration_attempts(source_url: str) -> list[dict[str, Any]]:
    parsed = urlsplit(source_url)
    attempts = [
        {
            "mode": "mirror",
            "snapshot_url": mirror_url(source_url),
            "allowed_domains": ("r.jina.ai",),
        }
    ]
    if parsed.netloc:
        attempts.append(
            {
                "mode": "direct",
                "snapshot_url": source_url,
                "allowed_domains": (parsed.netloc,),
            }
        )
    return attempts


def load_candidates(
    db_path: Path,
    allowed_sources: set[str],
    *,
    only_unhydrated: bool = False,
    query_term_stage: str = "",
) -> list[dict[str, Any]]:
    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    rows = backend.list_raw()
    backend.close()

    hydrated_keys: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("collection_stage") == "hydrated_page":
            hydrated_keys.add((str(row.get("source_name") or ""), str(row.get("source_url") or "")))

    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        source_name = str(row.get("source_name") or "")
        source_url = str(row.get("source_url") or "")
        if row.get("collection_stage") == "hydrated_page":
            continue
        if str(row.get("tail_rule_status") or "").strip().lower() == "terminal_skip":
            continue
        if allowed_sources and source_name not in allowed_sources:
            continue
        if query_term_stage and str(row.get("query_term_stage") or "").strip().lower() != query_term_stage.strip().lower():
            continue
        if not row.get("search_query_url") or not source_url.startswith(("http://", "https://")):
            continue
        key = (source_name, source_url)
        if only_unhydrated and key in hydrated_keys:
            continue
        if key in seen:
            continue
        seen.add(key)
        candidates.append(row)
    return candidates


def build_hydrated_payload(original: dict[str, Any], hydrated_row: dict[str, Any], mirror_snapshot_url: str) -> dict[str, Any]:
    payload = dict(hydrated_row)
    payload["source_type"] = original.get("source_type") or hydrated_row.get("source_type")
    payload["source_name"] = original.get("source_name") or hydrated_row.get("source_name")
    payload["source_url"] = original.get("source_url") or hydrated_row.get("source_url")
    payload["capture_snapshot_uri"] = mirror_snapshot_url
    payload["raw_payload_uri"] = mirror_snapshot_url
    payload["collector_version"] = "result_url_hydrator_v1"
    payload["collection_stage"] = "hydrated_page"
    payload["search_query_url"] = original.get("search_query_url")
    payload["search_query"] = original.get("search_query")
    payload["query_theme"] = original.get("query_theme")
    payload["query_term"] = original.get("query_term")
    payload["query_term_stage"] = original.get("query_term_stage") or hydrated_row.get("query_term_stage")
    payload["query_variant_index"] = original.get("query_variant_index")
    payload["result_title"] = original.get("result_title")
    payload["result_rank"] = original.get("result_rank")
    payload["matched_keywords"] = hydrated_row.get("matched_keywords") or original.get("matched_keywords") or []
    payload["excluded_keywords"] = hydrated_row.get("excluded_keywords") or original.get("excluded_keywords") or []
    payload["matched_themes"] = hydrated_row.get("matched_themes") or original.get("matched_themes") or []
    payload["excluded_themes"] = hydrated_row.get("excluded_themes") or original.get("excluded_themes") or []
    payload["keyword_hit_count"] = hydrated_row.get("keyword_hit_count") or original.get("keyword_hit_count") or 0
    payload["relevance_version"] = hydrated_row.get("relevance_version") or original.get("relevance_version")
    payload["hydrated_from_trace_id"] = original.get("trace_id")
    return payload


def summarize_pipeline_result(result_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result_payload.get("status"),
        "input_count": result_payload.get("input_count"),
        "accepted_count": result_payload.get("accepted_count"),
        "dropped_count": result_payload.get("dropped_count"),
        "classification_count": result_payload.get("classification_count"),
        "entity_count": result_payload.get("entity_count"),
        "risk_clue_count": result_payload.get("risk_clue_count"),
        "playbook_count": result_payload.get("playbook_count"),
        "strategy_count": result_payload.get("strategy_count"),
    }


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def prefetch_tail_skip_reason(original: dict[str, Any]) -> str | None:
    source_name = str(original.get("source_name") or "")
    source_url = str(original.get("source_url") or "").lower()
    result_title = str(original.get("result_title") or "").lower()

    if source_name in SECOND_HAND_TAIL_SOURCES:
        if any(marker in source_url for marker in AUTH_GATE_URL_MARKERS):
            return "auth_gate_listing"
        if "/carousell-account/q/" in source_url or "/verified-account/q/" in source_url:
            return "listing_page_noise"
        if "/p/" in source_url:
            return "auth_gated_product_page"
        if "login to your carousell account" in result_title or "signup or register your carousell account" in result_title:
            return "auth_gate_listing"

    if source_name in TIEBA_FORUM_TAIL_SOURCES:
        if "/f/good" in source_url and "精品贴" in result_title:
            return "generic_forum_index"

    return None


def targeted_tail_skip_reason(original: dict[str, Any], hydrated_row: dict[str, Any]) -> str | None:
    source_name = str(original.get("source_name") or "")
    source_url = str(original.get("source_url") or "").lower()
    result_title = str(original.get("result_title") or hydrated_row.get("result_title") or "")
    text = _normalized_text(hydrated_row.get("content_text") or "")

    if source_name in SECOND_HAND_TAIL_SOURCES:
        if any(marker in source_url for marker in AUTH_GATE_URL_MARKERS):
            return "auth_gate_listing"
        if any(marker in text for marker in SECURITY_VERIFICATION_MARKERS):
            return "security_verification_gate"

    if source_name in TIEBA_FORUM_TAIL_SOURCES:
        probe_terms = [
            str(original.get("query_term") or ""),
            *(str(item) for item in (original.get("matched_keywords") or [])),
            *(str(item) for item in (original.get("matched_themes") or [])),
        ]
        if any(marker in result_title for marker in TIEBA_GENERIC_FORUM_MARKERS):
            normalized_probe_terms = [term.strip().lower() for term in probe_terms if term.strip()]
            if normalized_probe_terms and not any(term in text for term in normalized_probe_terms):
                return "generic_forum_noise"

    return None


def main() -> int:
    args = parse_args()
    db_path = (PROJECT_ROOT / args.db).resolve() if not Path(args.db).is_absolute() else Path(args.db).resolve()
    allowed_sources = {item.strip() for item in str(args.source_names).split(",") if item.strip()}
    candidates = load_candidates(
        db_path,
        allowed_sources,
        only_unhydrated=args.only_unhydrated,
        query_term_stage=str(args.query_term_stage or ""),
    )
    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]

    backend = connect(f"sqlite:///{db_path.as_posix()}")
    backend.create_schema()
    hydrated_count = 0
    hydrated_rows: list[dict[str, Any]] = []
    hydrated_source_counter: Counter[str] = Counter()
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in candidates:
        source_url = str(row.get("source_url"))
        prefetch_skip_reason = prefetch_tail_skip_reason(row)
        if prefetch_skip_reason:
            updated_row = dict(row)
            updated_row["tail_rule_status"] = "terminal_skip"
            updated_row["tail_rule_reason"] = prefetch_skip_reason
            updated_row["tail_rule_version"] = TERMINAL_TAIL_RULE_VERSION
            backend.save_raw(updated_row)
            skipped.append({"source_name": row.get("source_name"), "source_url": source_url, "reason": prefetch_skip_reason})
            continue
        try:
            fetched: list[dict[str, Any]] = []
            snapshot_url = ""
            attempt_errors: list[str] = []
            for attempt in hydration_attempts(source_url):
                snapshot_url = str(attempt["snapshot_url"])
                try:
                    collector = HTTPFeedCollector(
                        HTTPFeedConfig(
                            source_url=snapshot_url,
                            source_name=str(row.get("source_name") or "hydrated_page"),
                            source_type=str(row.get("source_type") or "Social"),
                            legal_basis=str(row.get("legal_basis") or "PUBLIC_COMPLIANT_DATA"),
                            feed_format="html",
                            allowed_domains=tuple(attempt["allowed_domains"]),
                            include_keywords=tuple(str(item) for item in (row.get("matched_keywords") or []) if str(item).strip()),
                            exclude_keywords=tuple(str(item) for item in (row.get("excluded_keywords") or []) if str(item).strip()),
                            include_themes=tuple(str(item) for item in (row.get("matched_themes") or []) if str(item).strip()),
                            exclude_themes=tuple(str(item) for item in (row.get("excluded_themes") or []) if str(item).strip()),
                            search_query=str(row.get("search_query") or "").strip() or None,
                            query_theme=str(row.get("query_theme") or "").strip() or None,
                            query_term=str(row.get("query_term") or "").strip() or None,
                            query_variant_index=int(row.get("query_variant_index")) if row.get("query_variant_index") is not None else None,
                            min_keyword_hits=1,
                            timeout_seconds=args.timeout_seconds,
                            rate_limit_per_minute=args.rate_limit_per_minute,
                            retry_attempts=args.retry_attempts,
                            retry_backoff_seconds=args.retry_backoff_seconds,
                            retry_backoff_multiplier=args.retry_backoff_multiplier,
                            network_enabled=True,
                        )
                    )
                    fetched = [model_dump(item) for item in collector.collect()]
                    if fetched:
                        break
                except Exception as exc:
                    attempt_errors.append(f"{attempt['mode']}:{exc}")
            if not fetched:
                if attempt_errors:
                    raise RuntimeError("; ".join(attempt_errors))
                continue
            skip_reason = targeted_tail_skip_reason(row, fetched[0])
            if skip_reason:
                updated_row = dict(row)
                updated_row["tail_rule_status"] = "terminal_skip"
                updated_row["tail_rule_reason"] = skip_reason
                updated_row["tail_rule_version"] = TERMINAL_TAIL_RULE_VERSION
                backend.save_raw(updated_row)
                skipped.append({"source_name": row.get("source_name"), "source_url": source_url, "reason": skip_reason})
                continue
            payload = build_hydrated_payload(row, fetched[0], snapshot_url)
            backend.save_raw(payload)
            hydrated_count += 1
            hydrated_rows.append(payload)
            hydrated_source_counter[str(payload.get("source_name") or "unknown")] += 1
        except Exception as exc:  # keep best-effort hydration moving
            failed.append({"source_name": row.get("source_name"), "source_url": source_url, "error": str(exc)})

    pipeline_summary: dict[str, Any] | None = None
    if args.run_pipeline and hydrated_rows:
        result = PhaseTwoThreeEngine().run(hydrated_rows)
        result_payload = result.model_dump()
        for entity in result_payload.get("entities", []):
            backend.save_entity(entity)
        for strategy in result_payload.get("strategies", []):
            backend.append_audit(
                {
                    "event_type": "candidate_strategy_generated",
                    "actor": "public_hydration_pipeline",
                    "target_id": strategy.get("strategy_id"),
                    "payload": strategy,
                }
            )
        backend.append_audit(
            {
                "event_type": "public_hydration_pipeline_completed",
                "actor": "public_hydration_pipeline",
                "target_id": "hydrated_page_batch",
                "payload": {
                    "candidate_count": len(candidates),
                    "hydrated_count": hydrated_count,
                    "source_names": sorted(hydrated_source_counter),
                    **summarize_pipeline_result(result_payload),
                },
            }
        )
        pipeline_summary = summarize_pipeline_result(result_payload)

    backend.close()
    print(
        json.dumps(
            {
                "status": "completed",
                "db_path": str(db_path),
                "candidate_count": len(candidates),
                "hydrated_count": hydrated_count,
                "hydrated_source_counts": dict(sorted(hydrated_source_counter.items())),
                "pipeline_result": pipeline_summary,
                "skipped_count": len(skipped),
                "skipped": skipped[:20],
                "failed_count": len(failed),
                "failed": failed[:20],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
