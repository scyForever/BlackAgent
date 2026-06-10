"""Query-level benchmark evaluation for clue retrieval outputs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = PROJECT_ROOT / "src"
for path in (PROJECT_ROOT, PROJECT_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    target = _project_path(path)
    if not target.exists():
        return rows
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def evaluate_query_benchmark(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate natural-language queries against returned clue lists."""

    per_query: list[dict[str, Any]] = []
    latency_values: list[float] = []
    top_k_hits = 0
    total_matched = 0
    complete_evidence = 0
    reviewable = 0

    for index, raw_row in enumerate(rows):
        row = dict(raw_row)
        query = str(row.get("query") or "").strip()
        top_k = _positive_int(row.get("top_k"), default=5)
        expected = _list_of_dicts(row.get("expected_clues"))
        returned = _returned_clues(row)[:top_k]
        latency_ms = _optional_float(row.get("latency_ms"))
        if latency_ms is not None:
            latency_values.append(latency_ms)

        matched_pairs = _match_expected_clues(expected, returned)
        matched_count = len(matched_pairs)
        query_hit = matched_count > 0
        if query_hit:
            top_k_hits += 1
        for expected_clue, actual_clue in matched_pairs:
            total_matched += 1
            if _has_complete_evidence(expected_clue, actual_clue):
                complete_evidence += 1
            if _is_human_reviewable(actual_clue):
                reviewable += 1

        per_query.append(
            {
                "query_index": index,
                "query": query,
                "top_k": top_k,
                "expected_clue_count": len(expected),
                "returned_clue_count": len(returned),
                "matched_expected_clue_count": matched_count,
                "top_k_accuracy": 1.0 if query_hit else 0.0,
                "evidence_completeness_rate": _safe_rate(
                    sum(1 for expected_clue, actual_clue in matched_pairs if _has_complete_evidence(expected_clue, actual_clue)),
                    matched_count,
                ),
                "human_reviewability_rate": _safe_rate(
                    sum(1 for _expected_clue, actual_clue in matched_pairs if _is_human_reviewable(actual_clue)),
                    matched_count,
                ),
                "latency_ms": latency_ms,
                "matched_clue_ids": [str(actual.get("clue_id") or actual.get("key") or "") for _expected, actual in matched_pairs],
            }
        )

    query_count = len(per_query)
    return {
        "status": "completed" if query_count else "no_data",
        "evaluation_mode": "replayed_query_output_fixture",
        "latency_source": "benchmark_row_latency_ms",
        "query_count": query_count,
        "top_k_hits": top_k_hits,
        "top_k_accuracy": _safe_rate(top_k_hits, query_count),
        "matched_expected_clue_count": total_matched,
        "evidence_completeness_rate": _safe_rate(complete_evidence, total_matched),
        "human_reviewability_rate": _safe_rate(reviewable, total_matched),
        "latency": _latency_summary(latency_values),
        "per_query": per_query,
        "metric_definitions": {
            "top_k_accuracy": "share of natural-language queries with at least one expected clue in top_k results",
            "evidence_completeness_rate": "matched clues whose expected evidence traces and review evidence are present",
            "human_reviewability_rate": "matched clues with source snippets, classification, and entity evidence for analyst review",
        },
        "match_policy": "all_declared_expected_fields_must_match_for_top_k_hit",
        "claim_boundary": (
            "Evaluates natural-language query outputs supplied in benchmark rows; it scores query-level retrieval artifacts "
            "but does not execute live source collection or live retrieval during this evaluator run."
        ),
    }


def quality_gate_failures(report: Mapping[str, Any], args: argparse.Namespace) -> list[str]:
    failures: list[str] = []
    top_k = float(report.get("top_k_accuracy") or 0.0)
    evidence = float(report.get("evidence_completeness_rate") or 0.0)
    reviewability = float(report.get("human_reviewability_rate") or 0.0)
    latency = float((report.get("latency") or {}).get("p95_latency_ms") or 0.0)

    if args.min_top_k_accuracy is not None and top_k < float(args.min_top_k_accuracy):
        failures.append(f"top_k_accuracy_below_threshold:{top_k}<{args.min_top_k_accuracy}")
    if args.min_evidence_completeness_rate is not None and evidence < float(args.min_evidence_completeness_rate):
        failures.append(
            f"evidence_completeness_rate_below_threshold:{evidence}<{args.min_evidence_completeness_rate}"
        )
    if args.min_human_reviewability_rate is not None and reviewability < float(args.min_human_reviewability_rate):
        failures.append(
            f"human_reviewability_rate_below_threshold:{reviewability}<{args.min_human_reviewability_rate}"
        )
    if args.max_p95_latency_ms is not None and latency > float(args.max_p95_latency_ms):
        failures.append(f"p95_latency_ms_above_threshold:{latency}>{args.max_p95_latency_ms}")
    return failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate query-level clue retrieval quality.")
    parser.add_argument("--benchmark", default="tests/evaluation/query_level_benchmark.jsonl")
    parser.add_argument("--output", default=None)
    parser.add_argument("--min-top-k-accuracy", type=float, default=None)
    parser.add_argument("--min-evidence-completeness-rate", type=float, default=None)
    parser.add_argument("--min-human-reviewability-rate", type=float, default=None)
    parser.add_argument("--max-p95-latency-ms", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = evaluate_query_benchmark(load_jsonl(args.benchmark))
    failures = quality_gate_failures(report, args)
    report["quality_gate_failures"] = failures
    if failures:
        report["status"] = "failed_quality_gate"
    if args.output:
        output = _project_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _match_expected_clues(
    expected: list[dict[str, Any]],
    returned: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    used_actual: set[int] = set()
    for expected_clue in expected:
        best_index = None
        best_score = 0
        for index, actual_clue in enumerate(returned):
            if index in used_actual:
                continue
            score = _match_score(expected_clue, actual_clue)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index is not None and best_score >= _required_score(expected_clue):
            used_actual.add(best_index)
            pairs.append((expected_clue, returned[best_index]))
    return pairs


def _match_score(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> int:
    score = 0
    for field in ("clue_type", "key", "risk_category"):
        expected_value = _norm(expected.get(field))
        if expected_value and expected_value == _norm(actual.get(field)):
            score += 1
    expected_entities = {_norm(item) for item in expected.get("expected_entity_values") or [] if _norm(item)}
    actual_entities = _actual_entity_values(actual)
    if expected_entities and expected_entities.issubset(actual_entities):
        score += 1
    expected_traces = {_norm(item) for item in expected.get("expected_evidence_trace_ids") or [] if _norm(item)}
    actual_traces = {_norm(item) for item in actual.get("evidence_trace_ids") or [] if _norm(item)}
    if expected_traces and expected_traces.issubset(actual_traces):
        score += 1
    return score


def _required_score(expected: Mapping[str, Any]) -> int:
    fields = sum(1 for field in ("clue_type", "key", "risk_category") if _norm(expected.get(field)))
    if expected.get("expected_entity_values"):
        fields += 1
    if expected.get("expected_evidence_trace_ids"):
        fields += 1
    return max(1, fields)


def _has_complete_evidence(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    expected_traces = {_norm(item) for item in expected.get("expected_evidence_trace_ids") or [] if _norm(item)}
    actual_traces = {_norm(item) for item in actual.get("evidence_trace_ids") or [] if _norm(item)}
    if expected_traces and not expected_traces.issubset(actual_traces):
        return False
    reviewability = actual.get("evidence_reviewability") if isinstance(actual.get("evidence_reviewability"), Mapping) else {}
    has_snippets = bool(reviewability.get("original_snippets") or _evidence_cards(actual))
    has_time = bool(reviewability.get("time_range") or actual.get("time_range") or actual.get("last_seen") or actual.get("created_at"))
    has_sources = int(reviewability.get("source_count") or len(actual.get("source_names") or [])) >= int(expected.get("min_source_count") or 1)
    return bool(actual_traces and has_snippets and has_time and has_sources)


def _is_human_reviewable(actual: Mapping[str, Any]) -> bool:
    cards = _evidence_cards(actual)
    if not cards:
        return False
    for card in cards:
        if not isinstance(card, Mapping):
            continue
        if not (card.get("raw_snippet") or card.get("clean_text")):
            continue
        if not isinstance(card.get("classification"), Mapping):
            continue
        if not card.get("entities"):
            continue
        return True
    return False


def _returned_clues(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    for field in ("returned_clues", "actual_clues", "top_clues", "clues"):
        values = _list_of_dicts(row.get(field))
        if values:
            return values
    response = row.get("response") if isinstance(row.get("response"), Mapping) else {}
    return _list_of_dicts(response.get("top_clues") or response.get("high_quality_clues") or response.get("candidate_clues"))


def _evidence_cards(actual: Mapping[str, Any]) -> list[dict[str, Any]]:
    reviewability = actual.get("evidence_reviewability") if isinstance(actual.get("evidence_reviewability"), Mapping) else {}
    return _list_of_dicts(reviewability.get("evidence_cards"))


def _actual_entity_values(actual: Mapping[str, Any]) -> set[str]:
    values = {_norm(item) for item in actual.get("entity_values") or [] if _norm(item)}
    for card in _evidence_cards(actual):
        for entity in _list_of_dicts(card.get("entities")):
            value = _norm(entity.get("normalized_value") or entity.get("raw_value") or entity.get("value"))
            if value:
                values.add(value)
    return values


def _latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_latency_ms": None, "p95_latency_ms": None, "max_latency_ms": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_latency_ms": round(_percentile(ordered, 0.50), 4),
        "p95_latency_ms": round(_percentile(ordered, 0.95), 4),
        "max_latency_ms": round(max(ordered), 4),
    }


def _percentile(ordered: list[float], percentile: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _safe_rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _project_path(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else PROJECT_ROOT / target


if __name__ == "__main__":
    raise SystemExit(main())
