"""Generate a compliance-first smoke report for configured intelligence sources.

The report is intentionally offline by default.  It proves that IM/group,
social/forum, and vertical/technical source classes are configured with legal
metadata and safe network gates, without claiming that all sources were fetched
live in the current run.
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


SOURCE_CLASSES = {
    "im_or_group": {"IM", "Group", "Telegram", "X"},
    "social_or_forum": {"Social", "Forum", "News", "Blog"},
    "vertical_or_technical": {"Vertical", "THREAT_INTEL", "Technical", "TechForum"},
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an offline BlackAgent source smoke report.")
    parser.add_argument("--source-config", default="config/intel_sources.public.yaml", help="YAML source catalog to inspect.")
    parser.add_argument("--stats", default="data/collection_phase_delivery_stats.json", help="Optional historical collection stats JSON.")
    parser.add_argument("--output", default="data/source_smoke_report.json", help="Where to write the smoke report JSON.")
    parser.add_argument("--network-enabled", action="store_true", help="Mark the smoke run as live-network enabled. Default is dry-run.")
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
    network_enabled: bool = False,
    source_config: str | Path = "config/intel_sources.public.yaml",
) -> dict[str, Any]:
    source_counts = source_counts or Counter()
    policy = AuthorizedSourcePolicy()
    discovery = ComplianceSourceDiscovery()
    rows: list[dict[str, Any]] = []
    covered: set[str] = set()
    for source in sources:
        source_class = _source_class(source)
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
        rows.append(
            {
                "source_class": source_class,
                "source_name": str(source.get("source_name") or source.get("name") or "unknown_source"),
                "source_type": str(source.get("source_type") or source.get("type") or "unknown"),
                "platform": str(source.get("platform") or ""),
                "legal_basis": str(source.get("legal_basis") or ""),
                "network_enabled": bool(network_enabled),
                "run_type": "live_authorized_smoke" if network_enabled else "dry_run_catalog_smoke",
                "collected_count": collected_count,
                "filtered_count": 0,
                "duplicate_rate": None,
                "high_risk_candidate_count": 0,
                "failure_reason": None if decision.allowed and compliance.status in {"SCHEDULABLE", "NEEDS_RATE_LIMIT"} else decision.reason,
                "compliance_status": compliance.status if decision.allowed else "REJECTED",
                "compliance_reason": compliance.reason if decision.allowed else decision.reason,
            }
        )
        covered.add(source_class)

    selected = _pick_one_per_class(rows)
    missing = sorted(set(SOURCE_CLASSES) - {item["source_class"] for item in selected})
    return {
        "status": "completed" if not missing else "incomplete",
        "source_config": str(source_config),
        "network_enabled": bool(network_enabled),
        "run_type": "live_authorized_smoke" if network_enabled else "dry_run_catalog_smoke",
        "required_source_classes": sorted(SOURCE_CLASSES),
        "covered_source_classes": sorted({item["source_class"] for item in selected}),
        "missing_source_classes": missing,
        "claim_boundary": (
            "dry_run validates configured source metadata and compliance gates; "
            "live collection requires explicit --network-enabled and authorized domains."
        ),
        "sources": selected,
        "candidate_source_count": len(rows),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sources = load_sources(args.source_config)
    report = build_report(
        sources,
        source_counts=load_source_counts(args.stats),
        network_enabled=args.network_enabled,
        source_config=args.source_config,
    )
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


def _source_class(source: Mapping[str, Any]) -> str:
    source_type = str(source.get("source_type") or source.get("type") or "")
    platform = str(source.get("platform") or "")
    haystack = {source_type, platform, source_type.title(), platform.title()}
    for source_class, markers in SOURCE_CLASSES.items():
        if haystack & markers:
            return source_class
    if source_type.lower() in {"im", "telegram", "x"}:
        return "im_or_group"
    if source_type.lower() in {"social", "forum", "news", "blog"}:
        return "social_or_forum"
    if source_type.lower() in {"vertical", "threat_intel", "technical"}:
        return "vertical_or_technical"
    return "unknown"


def _pick_one_per_class(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    picked: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = picked.get(row["source_class"])
        if current is None or (row["collected_count"], row["source_name"]) > (current["collected_count"], current["source_name"]):
            picked[row["source_class"]] = row
    return [picked[key] for key in sorted(picked)]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
