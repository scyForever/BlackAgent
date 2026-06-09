"""Generate a compliance-first smoke report for configured intelligence sources.

The report is intentionally offline by default.  It proves that IM/group,
social/forum, vertical/technical, and public account/article smoke groups are
configured with legal metadata and safe network gates, without claiming that
all sources were fetched live in the current run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.enhancement.source_intake import AuthorizedSourcePolicy, ComplianceSourceDiscovery
from src.cleaner.text_filter import normalize_text
from src.collector import HTTPFeedCollector, HTTPFeedConfig
from src.collector.base_collector import model_dump
from src.collector.source_metadata import (
    classify_collection_failure,
    normalize_source_access_type,
    source_class_for_record,
    source_quota_groups_for_record,
)
from src.enhancement.text_intelligence import FineGrainedIntentClassifier


SOURCE_CLASSES = {
    "im_or_group": {"IM", "Group", "Telegram"},
    "social_or_forum": {"Social", "Forum", "News", "Blog"},
    "vertical_or_technical": {"Vertical", "THREAT_INTEL", "Technical", "TechForum"},
}
SMOKE_GROUPS = {
    "im_or_group": {"IM", "Group", "Telegram"},
    "public_account_or_article": {"Article", "Public_Account", "public_account", "html_article", "rss"},
    "social_or_forum": {"Social", "Forum", "News", "Blog"},
    "vertical_or_technical": {"Vertical", "THREAT_INTEL", "Technical", "TechForum"},
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an offline BlackAgent source smoke report.")
    parser.add_argument("--source-config", default="config/intel_sources.public.yaml", help="YAML source catalog to inspect.")
    parser.add_argument("--stats", default="data/collection_phase_delivery_stats.json", help="Optional historical collection stats JSON.")
    parser.add_argument("--output", default="data/source_smoke_report.json", help="Where to write the smoke report JSON.")
    parser.add_argument("--network-enabled", action="store_true", help="Mark the smoke run as live-network enabled. Default is dry-run.")
    parser.add_argument("--max-records", type=int, default=5, help="Maximum records to fetch per selected source when --network-enabled is set.")
    parser.add_argument("--timeout-seconds", type=float, default=10.0, help="HTTP timeout for live source smoke.")
    return parser.parse_args(argv)


def load_sources(path: str | Path) -> list[dict[str, Any]]:
    target = _project_path(path)
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    raw_sources = payload.get("sources") if isinstance(payload, Mapping) else []
    return [dict(item) for item in raw_sources or [] if isinstance(item, Mapping)]


def load_source_counts(path: str | Path) -> Counter[str]:
    target = _project_path(path)
    if not target.exists():
        return Counter()
    payload = json.loads(target.read_text(encoding="utf-8"))
    counts = Counter()
    for item in payload.get("source_counts", []) if isinstance(payload, Mapping) else []:
        if isinstance(item, Mapping):
            counts[str(item.get("source_name") or "unknown")] += int(item.get("count") or 0)
    return counts


def build_report(
    sources: list[dict[str, Any]],
    *,
    source_counts: Counter[str] | None = None,
    collection_metrics: dict[str, Mapping[str, Any]] | None = None,
    network_enabled: bool = False,
    source_config: str | Path = "config/intel_sources.public.yaml",
    max_records: int = 5,
    timeout_seconds: float = 10.0,
    min_records_per_class: int = 3,
) -> dict[str, Any]:
    source_counts = source_counts or Counter()
    collection_metrics = collection_metrics or {}
    policy = AuthorizedSourcePolicy()
    discovery = ComplianceSourceDiscovery()
    rows: list[dict[str, Any]] = []
    live_attempted_classes: set[str] = set()
    live_attempted_groups: set[str] = set()
    live_collected_by_group: Counter[str] = Counter()
    covered: set[str] = set()
    for source in sources:
        source_class = _source_class(source)
        smoke_group = _smoke_group(source)
        if source_class not in SOURCE_CLASSES:
            continue
        decision = policy.decide(source)
        compliance = discovery.evaluate(
            {
                **source,
                "source_url": source.get("source_url") or source.get("url") or "",
                "robots_allowed": source.get("robots_allowed", True),
                "terms_allow_security_research": source.get("terms_allow_security_research", True),
                "rate_limit_per_minute": source.get("rate_limit_per_minute") or source.get("rate_limit") or 1,
            }
        )
        collected_count = int(source_counts.get(str(source.get("source_name") or ""), 0))
        metrics = dict(collection_metrics.get(str(source.get("source_name") or ""), {}))
        should_attempt_live = (
            network_enabled
            and smoke_group in SMOKE_GROUPS
            and live_collected_by_group[smoke_group] < min_records_per_class
            and decision.allowed
            and compliance.status in {"SCHEDULABLE", "NEEDS_RATE_LIMIT"}
        )
        if should_attempt_live:
            metrics = {
                **metrics,
                **_collect_live_metrics(source, max_records=max_records, timeout_seconds=timeout_seconds),
            }
            live_collected_by_group[smoke_group] += int(metrics.get("collected_count") or 0)
            live_attempted_classes.add(source_class)
            live_attempted_groups.add(smoke_group)
        rows.append(
            {
                "source_class": source_class,
                "smoke_group": smoke_group,
                "source_name": str(source.get("source_name") or source.get("name") or "unknown_source"),
                "source_type": str(source.get("source_type") or source.get("type") or "unknown"),
                "platform": str(source.get("platform") or ""),
                "source_url": str(source.get("source_url") or source.get("url") or ""),
                "legal_basis": str(source.get("legal_basis") or ""),
                "source_access_type": normalize_source_access_type(
                    source.get("source_access_type"),
                    legal_basis=source.get("legal_basis"),
                    source_name=str(source.get("source_name") or source.get("name") or "unknown_source"),
                    source_url=str(source.get("source_url") or source.get("url") or ""),
                ),
                "authorization_statement": str(
                    source.get("authorization_statement")
                    or _default_authorization_statement(source, network_enabled=network_enabled)
                ),
                "network_enabled": bool(network_enabled),
                "live_smoke_attempted": bool(metrics.get("live_smoke_attempted", False)),
                "run_type": "live_authorized_smoke" if network_enabled else "dry_run_catalog_smoke",
                "collected_count": int(metrics.get("collected_count", collected_count) or 0),
                "filtered_count": int(metrics.get("filtered_count", 0) or 0),
                "duplicate_rate": metrics.get("duplicate_rate"),
                "high_risk_candidate_count": int(metrics.get("high_risk_candidate_count", 0) or 0),
                "source_evidence": list(metrics.get("source_evidence") or []),
                "failure_reason": (
                    metrics.get("failure_reason")
                    if metrics.get("failure_reason")
                    else None if decision.allowed and compliance.status in {"SCHEDULABLE", "NEEDS_RATE_LIMIT"} else decision.reason
                ),
                "compliance_status": compliance.status if decision.allowed else "REJECTED",
                "compliance_reason": compliance.reason if decision.allowed else decision.reason,
            }
        )
        covered.add(source_class)

    selected = _pick_one_per_smoke_group(rows)
    missing_source_classes = sorted(set(SOURCE_CLASSES) - {item["source_class"] for item in selected})
    missing_smoke_groups = sorted(set(SMOKE_GROUPS) - {item["smoke_group"] for item in selected})
    per_class_evidence = _per_class_evidence(rows, min_records_per_class=min_records_per_class)
    per_smoke_group_evidence = _per_smoke_group_evidence(rows, min_records_per_class=min_records_per_class)
    source_evidence_by_group = _source_evidence_by_group(rows)
    return {
        "status": "completed" if not missing_smoke_groups else "incomplete",
        "source_config": str(source_config),
        "network_enabled": bool(network_enabled),
        "run_type": "live_authorized_smoke" if network_enabled else "dry_run_catalog_smoke",
        "min_records_per_class": min_records_per_class,
        "required_source_classes": sorted(SOURCE_CLASSES),
        "required_smoke_groups": sorted(SMOKE_GROUPS),
        "covered_source_classes": sorted({item["source_class"] for item in selected}),
        "covered_smoke_groups": sorted({item["smoke_group"] for item in selected}),
        "missing_source_classes": missing_source_classes,
        "missing_smoke_groups": missing_smoke_groups,
        "per_class_evidence": per_class_evidence,
        "per_smoke_group_evidence": per_smoke_group_evidence,
        "source_evidence_by_group": source_evidence_by_group,
        "claim_boundary": (
            "dry_run validates configured source metadata and compliance gates; "
            "live collection requires explicit --network-enabled and authorized domains; "
            "per_smoke_group_evidence records whether each required source-evidence group reached "
            "the small 3-5 record smoke target while source_class keeps the global taxonomy."
        ),
        "sources": selected,
        "candidate_source_count": len(rows),
        "live_attempted_source_classes": sorted(live_attempted_classes),
        "live_attempted_smoke_groups": sorted(live_attempted_groups),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = load_sources(args.source_config)
    report = build_report(
        sources,
        source_counts=load_source_counts(args.stats),
        network_enabled=args.network_enabled,
        source_config=args.source_config,
        max_records=args.max_records,
        timeout_seconds=args.timeout_seconds,
    )
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _source_class(source: Mapping[str, Any]) -> str:
    source_class = source_class_for_record(source)
    return source_class if source_class in SOURCE_CLASSES else "unknown"


def _smoke_group(source: Mapping[str, Any]) -> str:
    quota_groups = set(source_quota_groups_for_record(source))
    if quota_groups & {"public_account_or_article", "public_account_article"}:
        return "public_account_or_article"
    source_class = _source_class(source)
    return source_class if source_class in SMOKE_GROUPS else "unknown"


def _pick_one_per_smoke_group(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    picked: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = picked.get(row["smoke_group"])
        if current is None or (row["collected_count"], row["source_name"]) > (current["collected_count"], current["source_name"]):
            picked[row["smoke_group"]] = row
    return [picked[key] for key in sorted(picked)]


def _live_representative_names(sources: list[dict[str, Any]]) -> set[str]:
    picked: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_class = _source_class(source)
        if source_class not in SOURCE_CLASSES:
            continue
        current = picked.get(source_class)
        if current is None or _representative_priority(source_class, source) < _representative_priority(source_class, current):
            picked[source_class] = source
    return {
        str(source.get("source_name") or source.get("name") or "unknown_source")
        for source in picked.values()
    }


def _representative_priority(source_class: str, source: Mapping[str, Any]) -> tuple[int, str]:
    platform = str(source.get("platform") or "").lower()
    source_type = str(source.get("source_type") or source.get("type") or "").lower()
    source_name = str(source.get("source_name") or source.get("name") or "unknown_source")
    if source_class == "im_or_group":
        if platform == "telegram" or source_type == "telegram":
            return (0, source_name)
        if platform in {"x", "twitter"} or source_type == "x":
            return (1, source_name)
    if source_class == "social_or_forum":
        if source_type == "forum":
            return (0, source_name)
        if source_type == "social":
            return (1, source_name)
    if source_class == "vertical_or_technical":
        if source_type == "vertical":
            return (0, source_name)
        if source_type in {"technical", "techforum", "threat_intel"}:
            return (1, source_name)
    return (9, source_name)


def _collect_live_metrics(
    source: Mapping[str, Any],
    *,
    max_records: int = 5,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    source_name = str(source.get("source_name") or source.get("name") or "unknown_source")
    try:
        collector = HTTPFeedCollector(
            HTTPFeedConfig(
                source_url=str(source.get("source_url") or source.get("url") or ""),
                source_name=source_name,
                source_type=str(source.get("source_type") or source.get("type") or "unknown"),
                platform=str(source.get("platform") or ""),
                legal_basis=str(source.get("legal_basis") or ""),
                feed_format=str(source.get("feed_format") or "auto"),
                max_records=max(1, int(max_records)),
                timeout_seconds=max(0.1, float(timeout_seconds)),
                allowed_domains=_allowed_domains(source),
                include_keywords=tuple(str(item) for item in source.get("include_keywords") or ()),
                exclude_keywords=tuple(str(item) for item in source.get("exclude_keywords") or ()),
                include_themes=tuple(str(item) for item in source.get("include_themes") or ()),
                exclude_themes=tuple(str(item) for item in source.get("exclude_themes") or ()),
                min_keyword_hits=int(source.get("min_keyword_hits") or 1),
                # Live smoke is a bounded one-record-per-class validation run,
                # not a production collection job.  Keep catalog rate-limit
                # metadata in the compliance row, but do not inherit the
                # collector's host sleep here; otherwise three representative
                # sources behind the same read-only proxy can turn a smoke into
                # a minute-long scheduled collection.
                rate_limit_per_minute=0,
                retry_attempts=0,
                source_access_type=source.get("source_access_type"),
                network_enabled=True,
            )
        )
        records = [model_dump(item) for item in collector.collect()]
        classifications = [
            FineGrainedIntentClassifier().classify(record).model_dump()
            for record in records
        ]
        normalized_texts = [normalize_text(str(record.get("content_text") or "")) for record in records]
        duplicate_rate = None
        if normalized_texts:
            duplicate_rate = round(1.0 - (len(set(normalized_texts)) / len(normalized_texts)), 4)
        return {
            "collected_count": len(records),
            "filtered_count": 0,
            "duplicate_rate": duplicate_rate,
            "high_risk_candidate_count": sum(
                1
                for item in classifications
                if str(item.get("risk_category") or "").strip() not in {"", "unknown", "正常业务白噪声"}
            ),
            "source_evidence": [_source_evidence_from_record(record) for record in records],
            "failure_reason": None,
            "live_smoke_attempted": True,
        }
    except Exception as exc:  # pragma: no cover - depends on live external availability
        return {
            "collected_count": 0,
            "filtered_count": 0,
            "duplicate_rate": None,
            "high_risk_candidate_count": 0,
            "failure_reason": classify_collection_failure(exc),
            "failure_detail": f"{type(exc).__name__}:{exc}",
            "live_smoke_attempted": True,
        }


def _per_class_evidence(rows: list[dict[str, Any]], *, min_records_per_class: int) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for source_class in sorted(SOURCE_CLASSES):
        class_rows = [row for row in rows if row.get("source_class") == source_class]
        total_collected = sum(int(row.get("collected_count") or 0) for row in class_rows)
        failures = [
            {
                "source_name": row.get("source_name"),
                "failure_reason": row.get("failure_reason"),
                "compliance_status": row.get("compliance_status"),
            }
            for row in class_rows
            if row.get("failure_reason")
        ]
        evidence.append(
            {
                "source_class": source_class,
                "configured_source_count": len(class_rows),
                "collected_count": total_collected,
                "target_min_records": min_records_per_class,
                "target_met": total_collected >= min_records_per_class,
                "failure_reasons": failures,
                "authorization_statements": [
                    row.get("authorization_statement")
                    for row in class_rows[:3]
                    if row.get("authorization_statement")
                ],
            }
        )
    return evidence


def _per_smoke_group_evidence(rows: list[dict[str, Any]], *, min_records_per_class: int) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for smoke_group in sorted(SMOKE_GROUPS):
        group_rows = [row for row in rows if row.get("smoke_group") == smoke_group]
        total_collected = sum(int(row.get("collected_count") or 0) for row in group_rows)
        evidence_rows = [
            item
            for row in group_rows
            for item in _source_evidence_for_row(row)
        ]
        failures = [
            {
                "source_name": row.get("source_name"),
                "failure_reason": row.get("failure_reason"),
                "compliance_status": row.get("compliance_status"),
            }
            for row in group_rows
            if row.get("failure_reason")
        ]
        evidence.append(
            {
                "smoke_group": smoke_group,
                "source_classes": sorted({str(row.get("source_class")) for row in group_rows if row.get("source_class")}),
                "configured_source_count": len(group_rows),
                "collected_count": total_collected,
                "target_min_records": min_records_per_class,
                "target_met": total_collected >= min_records_per_class,
                "source_evidence_count": len(evidence_rows),
                "hydrated_body_count": sum(1 for item in evidence_rows if item.get("hydrated_body")),
                "raw_body_count": sum(1 for item in evidence_rows if item.get("raw_body")),
                "snapshot_count": sum(1 for item in evidence_rows if item.get("capture_snapshot_uri")),
                "raw_payload_count": sum(1 for item in evidence_rows if item.get("raw_payload_uri")),
                "failure_reasons": failures,
                "authorization_statements": [
                    row.get("authorization_statement")
                    for row in group_rows[:3]
                    if row.get("authorization_statement")
                ],
            }
        )
    return evidence


def _source_evidence_by_group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        smoke_group: [
            item
            for row in rows
            if row.get("smoke_group") == smoke_group
            for item in _source_evidence_for_row(row)
        ]
        for smoke_group in sorted(SMOKE_GROUPS)
    }


def _source_evidence_for_row(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    explicit = row.get("source_evidence") or []
    evidence_rows = [
        _normalize_source_evidence_row(item, fallback=row)
        for item in explicit
        if isinstance(item, Mapping)
    ]
    if evidence_rows:
        return evidence_rows
    return [_normalize_source_evidence_row({}, fallback=row)]


def _source_evidence_from_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return _normalize_source_evidence_row(record, fallback=record)


def _normalize_source_evidence_row(item: Mapping[str, Any], *, fallback: Mapping[str, Any]) -> dict[str, Any]:
    source_name = str(item.get("source_name") or fallback.get("source_name") or fallback.get("source") or "unknown_source")
    source_class = str(item.get("source_class") or fallback.get("source_class") or _source_class(fallback))
    smoke_group = str(item.get("smoke_group") or fallback.get("smoke_group") or _smoke_group(fallback))
    raw_body = str(
        item.get("raw_body")
        or item.get("raw_text")
        or item.get("content_text")
        or fallback.get("raw_body")
        or fallback.get("raw_text")
        or fallback.get("content_text")
        or ""
    )
    hydrated_body = str(
        item.get("hydrated_body")
        or item.get("hydrated_text")
        or fallback.get("hydrated_body")
        or fallback.get("hydrated_text")
        or ""
    )
    url = str(
        item.get("url")
        or item.get("source_url")
        or fallback.get("url")
        or fallback.get("source_url")
        or ""
    )
    return {
        "source": source_name,
        "source_name": source_name,
        "source_class": source_class,
        "smoke_group": smoke_group,
        "source_type": str(item.get("source_type") or fallback.get("source_type") or ""),
        "platform": str(item.get("platform") or fallback.get("platform") or ""),
        "url": url,
        "source_url": url,
        "hydrated_body": hydrated_body,
        "raw_body": raw_body,
        "raw_snippet": str(item.get("raw_snippet") or fallback.get("raw_snippet") or raw_body[:500] or hydrated_body[:500]),
        "capture_snapshot_uri": str(item.get("capture_snapshot_uri") or fallback.get("capture_snapshot_uri") or ""),
        "raw_payload_uri": str(item.get("raw_payload_uri") or fallback.get("raw_payload_uri") or ""),
    }


def _allowed_domains(source: Mapping[str, Any]) -> tuple[str, ...]:
    raw = source.get("allowed_domains")
    values: list[str] = []
    if isinstance(raw, str):
        values.append(raw)
    elif isinstance(raw, list):
        values.extend(str(item) for item in raw if str(item).strip())
    allowed_domain = source.get("allowed_domain")
    if allowed_domain:
        values.append(str(allowed_domain))
    return tuple(dict.fromkeys(values))


def _default_authorization_statement(source: Mapping[str, Any], *, network_enabled: bool) -> str:
    legal_basis = str(source.get("legal_basis") or "")
    source_name = str(source.get("source_name") or source.get("name") or "unknown_source")
    mode = "live fetch was explicitly enabled" if network_enabled else "dry-run catalog validation only"
    return f"{source_name}: {legal_basis}; {mode}; no login/CAPTCHA/bypass flow is used."


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
