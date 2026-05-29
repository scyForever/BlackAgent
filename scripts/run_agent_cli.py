"""Command-line runner for BlackAgent investigation agent.

Examples:
    python scripts/run_agent_cli.py --demo-sample
    python scripts/run_agent_cli.py --query "找近24小时接码群控线索" --demo-sample --force-real
    python scripts/run_agent_cli.py --query "复核高质量线索" --fixture-path data/items.jsonl --output data/result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_project_env_file, load_settings

DEFAULT_LOCAL_CORPUS_PATH = "data/cleaning_phase_high_risk_corpus.jsonl"
DEFAULT_LOCAL_CORPUS_LIMIT = 200
DEFAULT_SOURCE_CONFIG_CANDIDATES = (
    "config/intel_sources.blackgray.yaml",
    "config/intel_sources.public.yaml",
)


DEMO_RECORDS: list[dict[str, Any]] = [
    {
        "trace_id": "cli-demo-1",
        "source_name": "tg-cli-demo",
        "source_type": "IM",
        "legal_basis": "AUTHORIZED_PARTNER",
        "publish_time": "2026-05-28T01:00:00+08:00",
        "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第一条",
    },
    {
        "trace_id": "cli-demo-2",
        "source_name": "forum-cli-demo",
        "source_type": "Forum",
        "legal_basis": "PUBLIC_COMPLIANT_DATA",
        "publish_time": "2026-05-28T01:05:00+08:00",
        "content_text": "接码服务和群控工具组合售卖，TG:core01 复用相同落地域名 risk.example 第二条",
    },
    {
        "trace_id": "cli-demo-3",
        "source_name": "feed-cli-demo",
        "source_type": "THREAT_INTEL",
        "legal_basis": "THIRD_PARTY_AUTHORIZED_FEED",
        "publish_time": "2026-05-28T01:10:00+08:00",
        "content_text": "群控脚本接码上车，联系 TG:core01，落地 https://risk.example/path，音符暗号 第三条",
    },
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run BlackAgent investigation agent from CLI.")
    parser.add_argument("--config", default=None, help="Optional config YAML path.")
    parser.add_argument("--query", "-q", default="", help="Investigation request. If omitted, CLI prompts for input.")
    parser.add_argument("--fixture-path", help="JSON or JSONL records to pass as fixture_items.")
    parser.add_argument(
        "--local-corpus-path",
        default=DEFAULT_LOCAL_CORPUS_PATH,
        help=(
            "Project-local JSON/JSONL corpus used automatically when no fixture, "
            "content text, demo sample, or source config is provided."
        ),
    )
    parser.add_argument(
        "--local-corpus-limit",
        type=int,
        default=DEFAULT_LOCAL_CORPUS_LIMIT,
        help="Maximum local corpus records to auto-load for a bare query.",
    )
    parser.add_argument(
        "--no-local-corpus",
        action="store_true",
        help="Disable automatic local corpus fallback for bare query-only runs.",
    )
    parser.add_argument(
        "--no-auto-source-config",
        action="store_true",
        help="Disable automatic discovery of config/intel_sources*.yaml for bare query-only runs.",
    )
    parser.add_argument(
        "--content-text",
        action="append",
        default=[],
        help="Add one raw intelligence text record. Can be passed multiple times.",
    )
    parser.add_argument("--demo-sample", action="store_true", help="Use built-in sample black/gray records.")
    parser.add_argument("--source-config-path", help="Optional source catalog path for local source selection/collection.")
    parser.add_argument(
        "--enable-network",
        action="store_true",
        help="Enable authorized HTTP feed collection for this CLI run. Source catalogs are only executed when network is enabled.",
    )
    parser.add_argument(
        "--max-sources",
        type=int,
        default=None,
        help="Maximum sources selected by the planner. Omit to use all authorized sources by default.",
    )
    parser.add_argument(
        "--routing-profile",
        choices=["fast", "balanced", "high_recall"],
        default=None,
        help="Investigation tradeoff profile: fast favors latency, high_recall favors coverage.",
    )
    parser.add_argument(
        "--max-raw-records",
        type=int,
        default=None,
        help="Request-scoped cap for raw records processed by the investigation.",
    )
    parser.add_argument(
        "--max-candidate-clues",
        type=int,
        default=None,
        help="Request-scoped cap for candidate clue retrieval/merge.",
    )
    parser.add_argument(
        "--max-llm-refine-clues",
        type=int,
        default=None,
        help="Request-scoped cap for top clue cards sent to LLM refinement.",
    )
    parser.add_argument(
        "--max-elapsed-seconds",
        type=int,
        default=None,
        help="Soft request-scoped elapsed-time budget for live collection/refinement.",
    )
    parser.add_argument(
        "--disable-live-collection",
        action="store_true",
        help="Disable live source collection for this request and use local/provided evidence only.",
    )
    parser.add_argument("--time-range-hours", type=int, default=None, help="Optional retrieval time-range filter.")
    parser.add_argument("--source-type", action="append", default=[], help="Optional source type filter; repeatable.")
    parser.add_argument("--risk-type", action="append", default=[], help="Optional risk type filter; repeatable.")
    parser.add_argument("--min-quality-score", type=float, default=None, help="Optional clue retrieval quality threshold.")
    parser.add_argument("--force-real", action="store_true", help="Override settings to enabled=true and dry_run=false.")
    parser.add_argument("--dry-run", action="store_true", help="Force LLM dry-run/mock-safe mode.")
    parser.add_argument("--model", default="", help="Override LLM model for this run.")
    parser.add_argument("--output", "-o", help="Write full JSON response to this file.")
    parser.add_argument(
        "--show",
        choices=["summary", "clues", "json"],
        default="summary",
        help="Console output shape.",
    )
    return parser.parse_args(argv)


def load_fixture_records(path: str | Path) -> list[dict[str, Any]]:
    fixture_path = Path(path)
    if not fixture_path.is_absolute():
        fixture_path = PROJECT_ROOT / fixture_path
    if not fixture_path.exists():
        raise FileNotFoundError(f"fixture file not found: {fixture_path}")

    text = fixture_path.read_text(encoding="utf-8")
    if fixture_path.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        loaded = json.loads(text)
        records = loaded if isinstance(loaded, list) else loaded.get("items") or loaded.get("fixture_items")
    if not isinstance(records, list):
        raise ValueError("fixture file must be a JSON list, {'items': [...]}, {'fixture_items': [...]}, or JSONL")
    return [dict(item) for item in records if isinstance(item, dict)]


def discover_source_config_path(
    *,
    config_dir: str | Path = PROJECT_ROOT / "config",
    candidates: tuple[str, ...] = DEFAULT_SOURCE_CONFIG_CANDIDATES,
) -> tuple[str | None, dict[str, Any]]:
    """Find the best project source catalog for a bare investigation query."""

    config_path = Path(config_dir)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    context: dict[str, Any] = {
        "mode": "source_config_auto_discovery",
        "source_config_auto_discovered": False,
        "source_config_path": None,
        "source_config_candidates": [],
    }

    ordered_paths: list[Path] = []
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = config_path / path.name if path.parts and path.parts[0] == "config" else PROJECT_ROOT / path
        ordered_paths.append(path)
    if config_path.exists():
        for path in sorted(config_path.glob("intel_sources*.yaml")):
            if path.name.endswith(".example.yaml"):
                continue
            if path not in ordered_paths:
                ordered_paths.append(path)

    for path in ordered_paths:
        if not path.exists() or not path.is_file():
            continue
        rel_path = _project_relative_path(path)
        context["source_config_candidates"].append(rel_path)
        if _looks_like_source_catalog(path):
            context["source_config_auto_discovered"] = True
            context["source_config_path"] = rel_path
            return rel_path, context

    context["skip_reason"] = "source_config_not_found"
    return None, context


def _looks_like_source_catalog(path: Path) -> bool:
    try:
        from src.config_loader import load_yaml_file

        payload = load_yaml_file(path)
    except Exception:
        return False
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        return False
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("source_name") and (source.get("source_url") or source.get("query_url_template")):
            return True
    return False


def _project_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)


def load_local_corpus_records(
    query: str,
    *,
    corpus_path: str | Path = DEFAULT_LOCAL_CORPUS_PATH,
    limit: int = DEFAULT_LOCAL_CORPUS_LIMIT,
    time_range_hours: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load recent, query-relevant records from the local delivery corpus.

    The CLI is often used interactively with only a natural-language query.
    Without explicit ``fixture_items`` or a source catalog the agent has no input
    to process, so this function safely seeds the run from the already-collected
    local high-risk corpus instead of returning an empty investigation result.
    """

    path = Path(corpus_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    context: dict[str, Any] = {
        "mode": "local_corpus_auto_seed",
        "corpus_path": str(path),
        "matched_count": 0,
        "loaded_count": 0,
        "time_range_hours": time_range_hours,
    }
    if limit <= 0:
        context["skip_reason"] = "local_corpus_limit_not_positive"
        return [], context
    if not path.exists():
        context["skip_reason"] = "local_corpus_not_found"
        return [], context

    try:
        raw_records = load_fixture_records(path)
    except Exception as exc:  # noqa: BLE001 - surface context to CLI output
        context["skip_reason"] = f"local_corpus_load_failed:{exc}"
        return [], context

    query_profile = _derive_query_profile(query)
    effective_hours = time_range_hours or query_profile.get("time_range_hours")
    if effective_hours:
        context["time_range_hours"] = effective_hours
    now = datetime.now(timezone.utc)
    matched: list[tuple[float, datetime | None, dict[str, Any]]] = []
    for record in raw_records:
        normalized = _normalize_local_corpus_record(record)
        if not normalized.get("content_text"):
            continue
        record_time = _record_reference_time(normalized)
        if effective_hours and record_time is not None and now - record_time > timedelta(hours=effective_hours):
            continue
        if not _record_matches_query(normalized, query_profile):
            continue
        score = _record_priority_score(normalized)
        matched.append((score, record_time, normalized))

    matched.sort(key=lambda item: (item[0], item[1] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    selected = [record for _score, _record_time, record in matched[:limit]]
    context["matched_count"] = len(matched)
    context["loaded_count"] = len(selected)
    context["risk_themes"] = query_profile.get("risk_themes", [])
    if not selected:
        context["skip_reason"] = "no_query_relevant_local_records"
    return selected, context


def _normalize_local_corpus_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    content_text = (
        normalized.get("content_text")
        or normalized.get("clean_text")
        or normalized.get("text")
        or normalized.get("raw_text")
        or ""
    )
    normalized["content_text"] = str(content_text)
    normalized.setdefault(
        "trace_id",
        normalized.get("source_trace_id")
        or normalized.get("trace_id")
        or normalized.get("hash_id")
        or normalized.get("clean_id"),
    )
    normalized.setdefault("source_trace_id", normalized.get("trace_id"))
    normalized.setdefault("source_name", normalized.get("source_name") or "local_delivery_corpus")
    normalized.setdefault("source_type", normalized.get("source_type") or "LOCAL_CORPUS")
    normalized.setdefault("legal_basis", normalized.get("legal_basis") or "PUBLIC_COMPLIANT_DATA")
    normalized.setdefault(
        "publish_time",
        normalized.get("publish_time") or normalized.get("created_at") or normalized.get("crawl_time"),
    )
    return normalized


def _derive_query_profile(query: str) -> dict[str, Any]:
    themes: list[str] = []
    normalized = query.lower()
    if "诈骗" in query or "引流" in query or "导流" in query or "私域" in query:
        themes.append("诈骗引流")
    if "接码" in query or "验证码" in query:
        themes.append("接码")
    if "刷单" in query or "补单" in query or "返佣" in query:
        themes.append("刷单作弊")
    if "账号" in query or "卖号" in query or "实名号" in query:
        themes.append("账号交易")
    if "群控" in query or "脚本" in query or "工具" in query:
        themes.append("工具交易")
    if "众包" in query or "代投" in query or "拉群" in query or "打粉" in query:
        themes.append("众包任务")

    time_range_hours: int | None = None
    if any(token in query for token in ("当天", "今日", "今天")) or "近24" in query or "24h" in normalized:
        time_range_hours = 24
    elif "48小时" in query or "48h" in normalized:
        time_range_hours = 48
    elif "72小时" in query or "72h" in normalized:
        time_range_hours = 72

    return {
        "risk_themes": _dedupe(themes),
        "raw_query": query,
        "time_range_hours": time_range_hours,
    }


def _record_matches_query(record: dict[str, Any], query_profile: dict[str, Any]) -> bool:
    themes = [str(item) for item in (query_profile.get("risk_themes") or []) if str(item).strip()]
    if not themes:
        return True

    record_theme_text = " ".join(
        str(value)
        for value in (
            *(record.get("risk_categories") or []),
            *(record.get("matched_themes") or []),
            record.get("query_theme") or "",
            *(record.get("risk_markers") or []),
        )
    )
    if any(theme in record_theme_text for theme in themes):
        return True

    try:
        from src.collector.relevance import decide_text_relevance

        decision = decide_text_relevance(record.get("content_text"), include_themes=themes)
        return decision.relevant
    except Exception:
        text = str(record.get("content_text") or "")
        return any(theme in text for theme in themes)


def _record_priority_score(record: dict[str, Any]) -> float:
    score = 0.0
    for field_name, weight in (("risk_score", 0.45), ("quality_score", 0.35), ("keyword_hit_count", 0.02)):
        try:
            score += float(record.get(field_name) or 0.0) * weight
        except (TypeError, ValueError):
            continue
    if record.get("source_url"):
        score += 0.05
    if record.get("matched_themes"):
        score += 0.05
    if record.get("risk_categories"):
        score += 0.05
    return round(score, 6)


def _record_reference_time(record: dict[str, Any]) -> datetime | None:
    for field_name in ("created_at", "publish_time", "crawl_time"):
        parsed = _parse_datetime(record.get(field_name))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result


def records_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if args.demo_sample:
        records.extend(DEMO_RECORDS)
    if args.fixture_path:
        records.extend(load_fixture_records(args.fixture_path))
    for index, content_text in enumerate(args.content_text, start=1):
        records.append(
            {
                "trace_id": f"cli-input-{index}",
                "source_name": "cli-input",
                "source_type": "Manual",
                "legal_basis": "PUBLIC_COMPLIANT_DATA",
                "content_text": content_text,
            }
        )
    return records


def apply_runtime_overrides(settings: Any, args: argparse.Namespace) -> None:
    if args.force_real:
        settings.llm.enabled = True
        settings.llm.dry_run = False
    if args.enable_network:
        settings.network.enabled = True
    if args.dry_run:
        settings.llm.dry_run = True
    if args.model:
        settings.llm.model = args.model


def policy_override_from_args(args: argparse.Namespace) -> dict[str, Any]:
    override: dict[str, Any] = {}
    if args.disable_live_collection:
        override["live_collection_enabled"] = False
    for arg_name, payload_name in (
        ("max_raw_records", "max_raw_records"),
        ("max_candidate_clues", "max_candidate_clues"),
        ("max_llm_refine_clues", "max_llm_refine_clues"),
        ("max_elapsed_seconds", "max_elapsed_seconds"),
    ):
        value = getattr(args, arg_name)
        if value is not None:
            override[payload_name] = value
    return override


def run_agent(payload: dict[str, Any], settings: Any) -> tuple[int, dict[str, Any]]:
    from src.local_runtime import LocalAgentRuntime

    runtime = LocalAgentRuntime(settings)
    try:
        body = runtime.run_investigation(
            payload["query"],
            fixture_items=payload.get("fixture_items") or (),
            fixture_path=payload.get("fixture_path"),
            source_config_path=payload.get("source_config_path"),
            sources=payload.get("sources") or (),
            max_sources=payload.get("max_sources"),
            time_range_hours=payload.get("time_range_hours"),
            source_types=payload.get("source_types") or (),
            risk_types=payload.get("risk_types") or (),
            min_quality_score=payload.get("min_quality_score"),
            routing_profile=payload.get("routing_profile"),
            policy_override=payload.get("policy_override"),
        )
    except Exception as exc:  # noqa: BLE001 - CLI should surface normalized local failures.
        return 1, {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
    finally:
        runtime.close()
    return 200, body


def print_summary(payload: dict[str, Any], *, show_clues: bool) -> None:
    print("\n=== BlackAgent Agent Result ===")
    print(f"status: {payload.get('status')}")
    print(f"mode: {payload.get('mode')}")
    print(f"query: {payload.get('query')}")
    print(f"input_count: {payload.get('input_count')}")
    print(f"high_quality_count: {payload.get('high_quality_count')}")
    print(f"candidate_count: {payload.get('candidate_count')}")
    cli_context = payload.get("cli_context") or {}
    if cli_context:
        print("\n--- cli_context ---")
        print(f"mode: {cli_context.get('mode')}")
        if cli_context.get("source_config_path"):
            print(f"source_config_path: {cli_context.get('source_config_path')}")
        if cli_context.get("source_config_status"):
            print(f"source_config_status: {cli_context.get('source_config_status')}")
        print(f"loaded_count: {cli_context.get('loaded_count')}")
        print(f"matched_count: {cli_context.get('matched_count')}")
        if cli_context.get("time_range_hours"):
            print(f"time_range_hours: {cli_context.get('time_range_hours')}")
        if cli_context.get("skip_reason"):
            print(f"skip_reason: {cli_context.get('skip_reason')}")

    execution_summary = payload.get("execution_summary") or {}
    if execution_summary:
        print("\n--- execution_summary ---")
        for key in (
            "status",
            "mode",
            "accepted_count",
            "dropped_count",
            "classification_count",
            "entity_count",
            "cluster_count",
            "risk_clue_count",
            "playbook_count",
            "strategy_count",
            "refined_clue_count",
        ):
            if key in execution_summary:
                print(f"{key}: {execution_summary[key]}")
        if execution_summary.get("live_collection_reasons"):
            print(f"live_collection_reasons: {execution_summary.get('live_collection_reasons')}")

    collection_runs = payload.get("collection_runs") or []
    if collection_runs:
        print("\n--- collection_runs ---")
        for item in collection_runs[:8]:
            line = (
                f"{item.get('source_name')}: "
                f"layer={item.get('collection_layer')} "
                f"fetched={item.get('fetched_count')}"
            )
            if item.get("error"):
                line += f" error={item.get('error')}"
            print(line)

    traces = payload.get("llm_traces") or []
    if traces:
        print("\n--- llm_traces ---")
        for item in traces:
            print(
                f"{item.get('stage')}: "
                f"llm_ok={item.get('llm_ok')} "
                f"used_fallback={item.get('used_fallback')} "
                f"error={item.get('error')}"
            )

    if show_clues:
        clues = list(payload.get("high_quality_clues") or []) + list(payload.get("candidate_clues") or [])
        print("\n--- clues ---")
        if not clues:
            print("(no clues)")
        for index, clue in enumerate(clues, start=1):
            refinement = clue.get("refinement") or {}
            quality = clue.get("quality") or {}
            print(f"\n[{index}] {clue.get('clue_id') or clue.get('key') or 'unknown'}")
            print(f"type: {clue.get('clue_type')}  key: {clue.get('key')}")
            print(f"confidence: {clue.get('confidence')}  quality_score: {clue.get('quality_score') or quality.get('score')}")
            summary = refinement.get("refined_summary") or clue.get("summary") or clue.get("description")
            if summary:
                print(f"summary: {summary}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_project_env_file()
    settings = load_settings(args.config)
    apply_runtime_overrides(settings, args)

    query = args.query.strip()
    if not query:
        query = input("请输入调查需求 query: ").strip()
    if not query:
        print("ERROR: query must not be empty", file=sys.stderr)
        return 2

    fixture_items = records_from_args(args)
    cli_context: dict[str, Any] | None = None
    has_explicit_records = bool(args.demo_sample or args.fixture_path or args.content_text)
    source_config_path = args.source_config_path
    if not has_explicit_records and not source_config_path and not args.no_auto_source_config:
        discovered_source_config_path, source_config_context = discover_source_config_path()
        cli_context = source_config_context
        if discovered_source_config_path and settings.network.enabled:
            source_config_path = discovered_source_config_path
            cli_context["source_config_status"] = "enabled_for_collection"
        elif discovered_source_config_path:
            cli_context["source_config_status"] = "discovered_but_network_disabled"
            cli_context["source_config_skip_reason"] = "network_disabled"

    should_auto_seed_local = (
        not has_explicit_records
        and not source_config_path
        and not args.no_local_corpus
    )
    if should_auto_seed_local:
        fixture_items, local_context = load_local_corpus_records(
            query,
            corpus_path=args.local_corpus_path,
            limit=args.local_corpus_limit,
            time_range_hours=args.time_range_hours,
        )
        if cli_context:
            local_context.update(
                {
                    "source_config_path": cli_context.get("source_config_path"),
                    "source_config_auto_discovered": cli_context.get("source_config_auto_discovered"),
                    "source_config_status": cli_context.get("source_config_status"),
                    "source_config_skip_reason": cli_context.get("source_config_skip_reason"),
                }
            )
        cli_context = local_context
    payload: dict[str, Any] = {
        "query": query,
        "fixture_items": fixture_items,
    }
    if args.max_sources is not None:
        payload["max_sources"] = args.max_sources
    if args.routing_profile:
        payload["routing_profile"] = args.routing_profile
    policy_override = policy_override_from_args(args)
    if policy_override:
        payload["policy_override"] = policy_override
    if source_config_path:
        payload["source_config_path"] = source_config_path
    if args.time_range_hours is not None:
        payload["time_range_hours"] = args.time_range_hours
    if args.source_type:
        payload["source_types"] = args.source_type
    if args.risk_type:
        payload["risk_types"] = args.risk_type
    if args.min_quality_score is not None:
        payload["min_quality_score"] = args.min_quality_score

    status_code, response_payload = run_agent(payload, settings)
    if cli_context is not None:
        response_payload["cli_context"] = cli_context
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {output_path}")

    if args.show == "json":
        print(json.dumps(response_payload, ensure_ascii=False, indent=2))
    else:
        print_summary(response_payload, show_clues=args.show == "clues")

    if status_code != 200:
        print(f"ERROR: local runtime status {status_code}", file=sys.stderr)
        return 1
    if response_payload.get("status") not in {"completed", "no_data"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
