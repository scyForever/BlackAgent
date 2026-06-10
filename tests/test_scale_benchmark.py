import pytest

from scripts import run_scale_benchmark
from scripts.run_scale_benchmark import parse_args, run_benchmark


def test_scale_benchmark_reports_cost_recall_and_default_defense_scales():
    report = run_benchmark(sample_sizes=[10, 20], batch_size=5, profile="fast")
    row = report["scenarios"][0]

    assert {
        "elapsed_seconds",
        "llm_call_count",
        "estimated_llm_tokens",
        "estimated_llm_cost_usd",
    } <= set(row)
    assert "clue_recall_proxy" in row
    assert "recall_change_vs_previous_scale" in report["scenarios"][1]
    assert report["default_defense_scales"] == [1000, 10000, 100000]


def test_scale_benchmark_recall_proxy_bounds_and_delta_shape():
    report = run_benchmark(sample_sizes=[10, 20], batch_size=5, profile="fast")
    first, second = report["scenarios"]

    assert 0.0 <= first["clue_recall_proxy"] <= 1.0
    assert 0.0 <= second["clue_recall_proxy"] <= 1.0
    assert first["recall_change_vs_previous_scale"] is None
    assert isinstance(second["recall_change_vs_previous_scale"], float)


def test_parse_args_defaults_and_help_include_default_defense_scales(capsys):
    assert parse_args([]).sample_sizes == [1000, 10000, 100000]

    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])

    assert exc_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "1000 10000 100000" in help_text


def test_run_benchmark_omitted_sample_sizes_uses_default_defense_scales(monkeypatch):
    monkeypatch.setattr(run_scale_benchmark, "_synthetic_records", lambda start, count: ())

    report = run_benchmark(batch_size=200_000, profile="fast")

    assert report["default_defense_scales"] == [1000, 10000, 100000]
    assert [row["sample_size"] for row in report["scenarios"]] == [1000, 10000, 100000]


def test_claim_boundary_states_deterministic_local_not_live_llm_proof():
    report = run_benchmark(sample_sizes=[10], batch_size=5, profile="fast")

    claim_boundary = report["claim_boundary"]
    assert "deterministic local throughput and routing-cost proof" in claim_boundary
    assert "not a live LLM latency or live LLM cost proof" in claim_boundary


def test_every_scenario_reports_cost_recall_and_recall_delta():
    report = run_benchmark(sample_sizes=[10, 20, 30], batch_size=5, profile="fast")

    for row in report["scenarios"]:
        assert "estimated_llm_cost_usd" in row
        assert "clue_recall_proxy" in row
        assert "recall_change_vs_previous_scale" in row
