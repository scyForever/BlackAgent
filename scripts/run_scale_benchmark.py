"""Run scale benchmarks for the deterministic core + model routing budget."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.agent.model_router import ModelRouter
from src.enhancement.text_intelligence import AdvancedEntityExtractor, FineGrainedIntentClassifier


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark BlackAgent core pipeline at 10k/100k scale.")
    parser.add_argument("--sample-sizes", nargs="+", type=int, default=[10_000, 100_000], help="Record counts to benchmark.")
    parser.add_argument("--batch-size", type=int, default=2_000, help="Batch size used for latency sampling.")
    parser.add_argument("--profile", default="fast", choices=["fast", "balanced", "high_recall"], help="Routing profile.")
    parser.add_argument("--output", default="data/scale_benchmark_report.json", help="Where to write JSON report.")
    return parser.parse_args(argv)


def run_benchmark(
    *,
    sample_sizes: Iterable[int] = (10_000, 100_000),
    batch_size: int = 2_000,
    profile: str = "fast",
) -> dict[str, Any]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    classifier = FineGrainedIntentClassifier()
    extractor = AdvancedEntityExtractor()
    router = _router_for_profile(profile)
    scenarios = []
    for sample_size in sample_sizes:
        if sample_size <= 0:
            raise ValueError("sample sizes must be positive")
        started = time.perf_counter()
        batch_latencies_ms: list[float] = []
        classified_count = entity_count = llm_call_count = estimated_tokens = review_required_count = 0
        route_reasons: dict[str, int] = {}
        for start in range(0, sample_size, batch_size):
            count = min(batch_size, sample_size - start)
            batch_started = time.perf_counter()
            for record in _synthetic_records(start, count):
                classification = classifier.classify(record).model_dump()
                entities = [item.model_dump() for item in extractor.extract({**record, "classification": classification})]
                entity_types = {str(item.get("entity_type") or "").lower() for item in entities}
                decision = router.decide_record(
                    rule_confidence=float(classification.get("confidence") or 0.0),
                    risk_score=0.82 if classification.get("risk_category") not in {"unknown", "正常业务白噪声"} else 0.2,
                    entity_count=len(entities),
                    has_contact=bool(entity_types.intersection({"contact", "account"})),
                    has_url=bool(entity_types.intersection({"url", "domain"})),
                    has_tool="tool_name" in entity_types,
                    has_conflict=bool(classification.get("conflict_status") == "CONFLICT_REVIEW" or classification.get("review_required")),
                    is_duplicate=False,
                    quality_score=0.82,
                )
                classified_count += 1
                entity_count += len(entities)
                review_required_count += int(bool(classification.get("review_required") or decision.requires_review))
                route_reasons[decision.reason] = route_reasons.get(decision.reason, 0) + 1
                if decision.action == "llm_classify_extract":
                    llm_call_count += 1
                    estimated_tokens += int(decision.max_tokens)
            batch_latencies_ms.append((time.perf_counter() - batch_started) * 1000.0 / max(count, 1))
        elapsed_seconds = time.perf_counter() - started
        scenarios.append(
            {
                "sample_size": sample_size,
                "classified_count": classified_count,
                "entity_count": entity_count,
                "review_required_count": review_required_count,
                "elapsed_seconds": round(elapsed_seconds, 4),
                "records_per_second": round(classified_count / max(elapsed_seconds, 1e-9), 2),
                "p50_record_latency_ms": round(statistics.median(batch_latencies_ms), 4),
                "p95_record_latency_ms": round(_percentile(batch_latencies_ms, 0.95), 4),
                "llm_call_count": llm_call_count,
                "llm_calls_per_1000_records": round(llm_call_count / max(classified_count, 1) * 1000.0, 4),
                "estimated_llm_tokens": estimated_tokens,
                "estimated_tokens_per_1000_records": round(estimated_tokens / max(classified_count, 1) * 1000.0, 4),
                "route_reasons": dict(sorted(route_reasons.items())),
            }
        )
    return {
        "status": "completed",
        "run_type": "scale_benchmark_core_routing",
        "profile": profile,
        "batch_size": batch_size,
        "scenarios": scenarios,
        "claim_boundary": (
            "Benchmark covers deterministic classification, entity extraction, and model-routing token budget. "
            "It intentionally excludes external live collection, LLM network latency, and entity-graph aggregation."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_benchmark(sample_sizes=args.sample_sizes, batch_size=args.batch_size, profile=args.profile)
    output = _project_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _router_for_profile(profile: str) -> ModelRouter:
    router = ModelRouter(profile=profile)
    if profile == "fast":
        return router.with_record_enrich_policy(
            enabled=False,
            reason="fast_profile_record_enrich_disabled_for_scale_benchmark",
            profile=profile,
            policy="disabled",
        )
    return router


def _synthetic_records(start: int, count: int) -> Iterable[dict[str, Any]]:
    templates = (
        "群控脚本接码上车，联系 TG:bench{index:06d}，落地 https://risk{index:06d}.example/path",
        "跑分代付车队收款，USDT 结算，暗号 code:b{index:06d}",
        "私域导流返利拉新，开户链接 https://lead{index:06d}.example/a，联系 TG:lead{index:06d}",
        "协议号自动注册后台配置，卡密授权，客服 TG:tool{index:06d}",
        "普通反诈研究文章，讨论接码风险但不提供交易入口",
    )
    for offset in range(count):
        index = start + offset
        yield {
            "trace_id": f"bench-{index:06d}",
            "source_name": f"scale-bench-{index % 11}",
            "source_type": "IM" if index % 3 == 0 else "Forum",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "content_text": templates[index % len(templates)].format(index=index),
        }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
