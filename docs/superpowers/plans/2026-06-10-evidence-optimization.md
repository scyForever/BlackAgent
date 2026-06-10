# Evidence Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the six current optimization points in `优化.md`: authorized real-source rerun proof, expanded held-out/source-holdout evaluation, agent-like slang review-to-rules closure, real OCR scenario reporting, cost/latency/recall benchmark proof, and clue-to-evidence-pack answer chain unification.

**Architecture:** Keep the work in existing artifact-builder scripts and evaluation reports. Each optimization must produce a local, reviewable JSON/JSONL artifact with a conservative claim boundary, plus focused pytest coverage proving the artifact contains the requested fields and does not overclaim live coverage without credentials.

**Tech Stack:** Python 3.11, pytest, JSON/JSONL artifacts, existing BlackAgent pipeline/evaluation/collector/OCR scripts.

---

## File Structure

- Create `scripts/build_authorized_source_rerun_pack.py`: aggregate real authorized source small-sample rerun evidence from raw JSONL and smoke/source reports.
- Create `tests/test_authorized_source_rerun_pack.py`: prove collection time, credential boundary, failure reasons, raw snapshot URI, and source coverage are present.
- Modify `scripts/build_heldout_eval.py`: add source/time/slang-family holdout coverage reporting.
- Modify `tests/test_evaluate_pipeline.py` or create `tests/test_heldout_coverage.py`: prove source-holdout, time-holdout, and slang-family holdout dimensions.
- Modify `scripts/build_slang_candidate_report.py`: export analyst-approved lifecycle records into a slang dictionary/rule overlay with evaluation gain metadata.
- Modify `tests/test_slang_candidate_lifecycle_export.py`: prove approved rows produce runtime dictionary update and pending/rejected rows do not.
- Modify `scripts/build_ocr_hardset.py`: add real-authorized OCR scene assessment for 30-50 screenshot/poster target, exact/substr metrics, entity impact, and failure samples.
- Modify `tests/test_ocr_manifest_import.py`: prove manifest reports include target coverage and failure/entity-impact metrics.
- Modify `scripts/run_scale_benchmark.py`: add 1k/10k/100k default scale rows, estimated token cost, and clue recall proxy fields.
- Create or modify `tests/test_scale_benchmark.py`: prove scale reports contain elapsed time, LLM calls, token cost, and clue recall change fields.
- Modify `scripts/build_acceptance_evidence_pack.py`: emit clue-centric evidence index that binds high-quality clues to evidence pack rows, raw snapshot, clean text, classification, entities, and optional graph relations.
- Modify `tests/test_acceptance_evidence_pack.py`: prove every high-quality clue can be followed to its evidence-pack chain.
- Modify `scripts/run_acceptance_gate.py`: include new authorized rerun, OCR hardset, scale benchmark, and clue evidence index artifacts in final summary when present.
- Modify `README.md` and delivery docs only after code artifacts are verified.

## Task 1: Authorized Real-Source Rerun Pack

**Files:**
- Create: `scripts/build_authorized_source_rerun_pack.py`
- Create: `tests/test_authorized_source_rerun_pack.py`
- Modify: `scripts/run_acceptance_gate.py`

- [ ] **Step 1: Write failing tests**

Add tests that call `build_pack()` with two source reports and three raw records. The expected report must contain:

```python
assert report["status"] == "completed"
assert report["credential_boundary"]["has_real_external_source"] is True
assert report["credential_boundary"]["loopback_only"] is False
assert report["source_coverage"]["covered_groups"]["real_telegram"] == 1
assert report["source_coverage"]["covered_groups"]["public_account_or_article"] == 1
assert report["snapshot_coverage"]["raw_snapshot_uri_count"] == 3
assert report["failure_summary"]["by_reason"]["login_required"] == 1
assert rows[0]["capture_snapshot_uri"].startswith("s3://snapshots/")
```

- [ ] **Step 2: Verify red**

Run:

```powershell
pytest tests/test_authorized_source_rerun_pack.py -q
```

Expected: FAIL because the script does not exist.

- [ ] **Step 3: Implement minimal artifact builder**

Implement `build_pack(raw_rows, source_reports, collection_started_at=None, collection_finished_at=None)` returning `{"rows": ..., "report": ...}`. Normalize source groups with existing `source_quota_groups_for_record()`, record `credential_boundary`, preserve `failure_reason`, `capture_snapshot_uri`, `raw_payload_uri`, `crawl_time`, and emit conservative claim boundaries for loopback/demo rows.

- [ ] **Step 4: Verify green**

Run:

```powershell
pytest tests/test_authorized_source_rerun_pack.py tests/test_acceptance_gate.py -q
```

Expected: PASS.

## Task 2: Held-Out Source/Time/Slang-Family Coverage

**Files:**
- Modify: `scripts/build_heldout_eval.py`
- Create: `tests/test_heldout_coverage.py`

- [ ] **Step 1: Write failing tests**

Create records spanning Telegram, secondhand, crowdsourcing, public account article, and forum rows with `publish_time` and slang markers. Assert:

```python
report = build_holdout_coverage_report(records, output_path="tests/evaluation/heldout_classification.jsonl")
assert report["holdout_dimensions"]["source_holdout"]["covered_required_groups"] >= {
    "real_telegram", "secondhand_market", "crowdsourcing_platform", "public_account_or_article"
}
assert report["holdout_dimensions"]["time_holdout"]["bucket_counts"]["recent_0_7d"] == 1
assert report["holdout_dimensions"]["slang_family_holdout"]["family_counts"]["telegram_alias"] == 1
assert report["claim_boundary"].startswith("Holdout coverage")
```

- [ ] **Step 2: Verify red**

Run:

```powershell
pytest tests/test_heldout_coverage.py -q
```

Expected: FAIL because `build_holdout_coverage_report` is missing.

- [ ] **Step 3: Implement coverage reporting**

Add `build_holdout_coverage_report(records, output_path)` and include its output under `build_report()` as `holdout_dimensions`. Source groups must include source quota groups plus `real_telegram` when source/platform/URL indicates Telegram. Time buckets: `recent_0_7d`, `mid_8_30d`, `older_31d_plus`, `missing_time`. Slang families: Telegram aliases, WeChat aliases, SMS-code aliases, group aliases, account-material aliases, and unknown/new slang families.

- [ ] **Step 4: Verify green**

Run:

```powershell
pytest tests/test_heldout_coverage.py tests/test_evaluate_pipeline.py -q
```

Expected: PASS.

## Task 3: Slang Review-To-Rules Closure

**Files:**
- Modify: `scripts/build_slang_candidate_report.py`
- Modify: `tests/test_slang_candidate_lifecycle_export.py`

- [ ] **Step 1: Write failing tests**

Extend lifecycle export tests:

```python
dictionary_update = slang_dictionary_update_from_lifecycle(lifecycle["records"])
assert dictionary_update["status"] == "completed"
assert dictionary_update["rules_version"]
assert dictionary_update["dictionary_patch"]["slang_dictionary"]["火苗"] == "WhatsApp"
assert dictionary_update["accepted_terms"][0]["evaluation_gain"]["primary_classification_f1_delta"] == 0.05
assert "影子词" not in dictionary_update["dictionary_patch"]["slang_dictionary"]
```

- [ ] **Step 2: Verify red**

Run:

```powershell
pytest tests/test_slang_candidate_lifecycle_export.py::test_slang_candidate_report_exports_review_csv_and_lifecycle_records -q
```

Expected: FAIL because dictionary update export is missing.

- [ ] **Step 3: Implement dictionary/rule overlay export**

Add `slang_dictionary_update_from_lifecycle(records, base_dictionary=None, rules_version=None)` and CLI `--dictionary-update-out`. Include only `GRAY_ROLLOUT` and `ACTIVE` records, keep pending/rejected excluded, preserve reviewer/version/batch/evaluation gain metadata, and write a YAML overlay compatible with `config/slang_dictionary.yaml` without silently editing production config.

- [ ] **Step 4: Verify green**

Run:

```powershell
pytest tests/test_slang_candidate_report.py tests/test_slang_candidate_lifecycle_export.py -q
```

Expected: PASS.

## Task 4: OCR Real-Scene Assessment

**Files:**
- Modify: `scripts/build_ocr_hardset.py`
- Modify: `tests/test_ocr_manifest_import.py`

- [ ] **Step 1: Write failing tests**

Extend manifest report tests to assert:

```python
assessment = report["real_scene_assessment"]
assert assessment["target_range"] == {"min": 30, "max": 50}
assert assessment["authorized_manifest_count"] == 2
assert assessment["coverage_status"] == "insufficient_real_authorized_screenshots"
assert assessment["entity_extraction_impact"]["expected_entity_count"] == 2
assert assessment["failure_samples"][0]["trace_id"] == "ocr-fail"
```

- [ ] **Step 2: Verify red**

Run:

```powershell
pytest tests/test_ocr_manifest_import.py -q
```

Expected: FAIL on the new assertions.

- [ ] **Step 3: Implement real scene section**

Add `real_scene_assessment` to `build_report()`. Keep existing report status behavior for backwards compatibility, but separately report target range, authorized manifest count, image kind coverage, exact/substr OCR quality, expected entity impact, OCR failure samples, and claim boundary.

- [ ] **Step 4: Verify green**

Run:

```powershell
pytest tests/test_ocr_manifest_import.py -q
```

Expected: PASS.

## Task 5: Cost/Latency/Recall Scale Benchmark

**Files:**
- Modify: `scripts/run_scale_benchmark.py`
- Create: `tests/test_scale_benchmark.py`

- [ ] **Step 1: Write failing tests**

Add a small benchmark test:

```python
report = run_benchmark(sample_sizes=[10, 20], batch_size=5, profile="fast")
row = report["scenarios"][0]
assert {"elapsed_seconds", "llm_call_count", "estimated_llm_tokens", "estimated_llm_cost_usd"} <= set(row)
assert "clue_recall_proxy" in row
assert "recall_change_vs_previous_scale" in report["scenarios"][1]
assert report["default_defense_scales"] == [1000, 10000, 100000]
```

- [ ] **Step 2: Verify red**

Run:

```powershell
pytest tests/test_scale_benchmark.py -q
```

Expected: FAIL on missing fields.

- [ ] **Step 3: Implement benchmark fields**

Change CLI defaults to `1000 10000 100000`. Add cost estimation, per-scale LLM call counts, token cost, deterministic clue-signal recall proxy, and recall delta vs previous scale. Keep the claim boundary clear that it is deterministic local throughput and routing-cost proof, not live LLM latency.

- [ ] **Step 4: Verify green**

Run:

```powershell
pytest tests/test_scale_benchmark.py -q
```

Expected: PASS.

## Task 6: Clue-Centric Evidence Chain Index

**Files:**
- Modify: `scripts/build_acceptance_evidence_pack.py`
- Modify: `tests/test_acceptance_evidence_pack.py`
- Modify: `scripts/run_acceptance_gate.py`

- [ ] **Step 1: Write failing tests**

Add a test that builds evidence rows with one high-quality clue and asserts:

```python
index = build_clue_evidence_index(evidence_rows, clues=clues)
assert index["report"]["high_quality_clue_count"] == 1
chain = index["rows"][0]["answer_chain"]
assert chain[0]["raw_snapshot"]["capture_snapshot_uri"] == "s3://snapshots/trace-1.html"
assert chain[0]["clean_text"] == "群控脚本引流 联系 TG:risk01"
assert chain[0]["classification"]["risk_category"] == "工具交易"
assert chain[0]["entities"][0]["normalized_value"] == "Telegram:risk01"
assert index["rows"][0]["clickable_chain_uri"].startswith("evidence-pack://clue/")
```

- [ ] **Step 2: Verify red**

Run:

```powershell
pytest tests/test_acceptance_evidence_pack.py::test_build_acceptance_evidence_pack_builds_clue_centric_answer_chain -q
```

Expected: FAIL because `build_clue_evidence_index` is missing.

- [ ] **Step 3: Implement clue index**

Implement `build_clue_evidence_index(evidence_rows, clues, graph_relations=None)` and CLI `--clue-index-output`. Treat clues with `quality_score >= 0.7` or `quality_level == "high"` as high quality; include raw snapshot/raw payload/source URL, clean text, classification, entities, clue metadata, and graph relations whose evidence traces overlap.

- [ ] **Step 4: Verify green**

Run:

```powershell
pytest tests/test_acceptance_evidence_pack.py tests/test_acceptance_gate.py -q
```

Expected: PASS.

## Final Verification

- [ ] Run focused optimization tests:

```powershell
pytest tests/test_authorized_source_rerun_pack.py tests/test_heldout_coverage.py tests/test_slang_candidate_lifecycle_export.py tests/test_ocr_manifest_import.py tests/test_scale_benchmark.py tests/test_acceptance_evidence_pack.py tests/test_acceptance_gate.py -q
```

- [ ] Run all offline tests:

```powershell
pytest -q
```

- [ ] Generate the main local proof artifacts:

```powershell
python scripts/run_scale_benchmark.py --sample-sizes 1000 10000 --batch-size 1000 --profile fast --output data/scale_benchmark_report.json
python scripts/run_acceptance_gate.py --skip-unit-tests --skip-network-smoke
```

Expected: commands exit 0, and every current `优化.md` item has direct code, test, and report evidence.
