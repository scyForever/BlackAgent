# BlackAgent Optimization Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the six optimization points listed in `优化.md`: classification quality, entity postprocessing, source quota balance, clue gold evaluation, slang lifecycle closure, and an end-to-end defense demo script/report.

**Architecture:** Keep optimizations in the existing deterministic pipeline: classifier rules and difficult fixtures drive intent quality, entity postprocessing filters delivery noise, source quota helpers balance collection, evaluation scripts produce clue/manual-gold evidence, slang lifecycle exports analyst-reviewed runtime records, and defense scripts aggregate a runnable demonstration. Each task has a narrow write scope and a targeted pytest command.

**Tech Stack:** Python 3.11+, pytest, JSONL evaluation fixtures, existing BlackAgent scripts and rule registry.

---

### Task 1: Classification Hard-Negative Quality

**Files:**
- Modify: `src/enhancement/text_intelligence.py`
- Modify: `config/risk_taxonomy.yaml` or `config/model_stage_policy.yaml` only if existing policy terms need extension.
- Test: `tests/test_optimization_boundaries.py`
- Test: `tests/evaluation/hard_negative.jsonl` if extra gold rows are needed.

- [ ] **Step 1: Write or extend failing tests**

```python
def test_manual_heldout_typical_errors_handle_tutorial_rebate_and_account_tool_conflicts():
    classifier = FineGrainedIntentClassifier()
    tutorial = classifier.classify({"content_text": "CK3 steam版MOD教程：如何开启自动招募功能，steam创意工坊订阅说明。"})
    rebate = classifier.classify({"content_text": "交易所高返佣账号开户链接，支持 API 高返佣和拉新，联系 TG:rebate001。"})
    hybrid = classifier.classify({"content_text": "用户端注册账号，免费赠送测试卡密，接码平台电脑端客服 @kefututu88。"})
    assert tutorial.risk_category == "正常业务白噪声"
    assert rebate.risk_category == "诈骗引流"
    assert hybrid.conflict_status == "CONFLICT_REVIEW"
```

- [ ] **Step 2: Verify red if adding new coverage**

Run: `pytest tests/test_optimization_boundaries.py::test_manual_heldout_typical_errors_handle_tutorial_rebate_and_account_tool_conflicts -q`

Expected: the new edge case fails before implementation, or existing coverage already passes and should be preserved.

- [ ] **Step 3: Implement minimum classifier calibration**

Prefer changes inside `FineGrainedIntentClassifier` helper methods: classify ordinary tutorial/game/mod/technical discussion as low relevance, preserve rebate/traffic intent, and keep account/tool overlaps in manual-review conflict.

- [ ] **Step 4: Verify targeted quality**

Run: `pytest tests/test_optimization_boundaries.py tests/test_evaluate_pipeline.py -q`

Expected: all selected tests pass and hard-negative FPR remains within the configured gate.

### Task 2: Entity Postprocessing At Delivery Exit

**Files:**
- Modify: `src/intelligence/entity_postprocessor.py`
- Test: `tests/test_phase23.py` or `tests/test_optimization_boundaries.py`

- [ ] **Step 1: Write a failing pseudo-entity test**

```python
def test_entity_postprocessor_drops_footer_and_template_entities():
    rows = [
        AdvancedEntity("url", "https://site.example/logo.png", "https://site.example/logo.png", 20, 50, "r1"),
        AdvancedEntity("slang_term", "Channel", "Channel", 70, 77, "r1"),
        AdvancedEntity("contact", "@riskops", "Telegram:riskops", 95, 103, "r1"),
    ]
    kept = filter_and_order_entities(rows, {"content_text": "Follow us Channel logo image 联系 @riskops 群控脚本"})
    assert [item.normalized_value for item in kept] == ["Telegram:riskops"]
```

- [ ] **Step 2: Verify red if adding new coverage**

Run: `pytest tests/test_phase23.py::test_entity_postprocessor_drops_footer_and_template_entities -q`

Expected: pseudo footer/template entities fail before implementation, or existing coverage already passes and should be preserved.

- [ ] **Step 3: Implement minimum postprocessor filters**

Extend pseudo-value and boilerplate URL detection only in `entity_postprocessor.py`. Keep high-value TG/WeChat/QQ, domains, accounts, invite codes, settlement methods, and tool names first in output order.

- [ ] **Step 4: Verify targeted entity behavior**

Run: `pytest tests/test_phase23.py tests/test_evaluate_pipeline.py -q`

Expected: pseudo entities are filtered and entity F1 stays above gate.

### Task 3: Source Quota Balance

**Files:**
- Modify: `src/collector/source_quota.py`
- Modify: `src/collector/source_metadata.py`
- Modify: `scripts/collect_public_sources.py`
- Test: `tests/test_optimization_boundaries.py`

- [ ] **Step 1: Write a failing quota test**

```python
def test_source_min_quota_prefers_vertical_secondhand_and_crowdsourcing_groups():
    selected = quota_balanced_source_slice(sources, max_sources=4, minimum_quotas={"vertical_or_technical": 1, "secondhand_market": 1, "crowdsourcing_platform": 1})
    groups = {group for source in selected for group in source_quota_groups_for_record(source)}
    assert {"vertical_or_technical", "secondhand_market", "crowdsourcing_platform"} <= groups
```

- [ ] **Step 2: Verify red if adding new coverage**

Run: `pytest tests/test_optimization_boundaries.py::test_source_min_quota_prefers_vertical_secondhand_and_crowdsourcing_groups -q`

Expected: underfilled granular groups fail before implementation, or existing coverage already passes and should be preserved.

- [ ] **Step 3: Implement minimum quota selection/reporting**

Use `apply_source_min_quotas()` and `source_quota_groups_for_record()` to enforce minimum vertical/technical, public-account/article, second-hand, and crowdsourcing source groups when `--max-sources` is used.

- [ ] **Step 4: Verify quota behavior**

Run: `pytest tests/test_optimization_boundaries.py tests/test_layered_collection.py -q`

Expected: selected sources include non-IM quota groups and report quota warnings when underfilled.

### Task 4: Clue Gold Evaluation

**Files:**
- Modify: `scripts/evaluate_pipeline.py`
- Modify: `tests/evaluation/manual_heldout_clues.jsonl` only if current fixture is below 20 reviewable clue rows.
- Test: `tests/test_evaluate_pipeline.py`

- [ ] **Step 1: Write or extend clue-gold tests**

```python
def test_manual_heldout_clue_gold_fixture_exists_with_evidence_chain_requirements():
    records = load_jsonl("tests/evaluation/manual_heldout_clues.jsonl")
    expected = [clue for record in records for clue in record.get("expected_clues", [])]
    assert len(expected) >= 20
    assert all(clue.get("expected_evidence_trace_ids") for clue in expected)
```

- [ ] **Step 2: Verify red if fixture is insufficient**

Run: `pytest tests/test_evaluate_pipeline.py::test_manual_heldout_clue_gold_fixture_exists_with_evidence_chain_requirements -q`

Expected: fixture-size/evidence-chain gap fails before implementation, or current fixture already satisfies the stronger gate.

- [ ] **Step 3: Implement evaluation reporting**

Ensure `evaluate_clues()` reports clue precision/recall, duplicate clue rate, evidence-chain precision/recall, and reviewability rate for `expected_clues` objects.

- [ ] **Step 4: Verify clue evaluation**

Run: `pytest tests/test_evaluate_pipeline.py -q`

Expected: clue metrics are present and manual held-out clue gold can be used as evidence instead of `not_applicable_no_gold`.

### Task 5: Slang Candidate Lifecycle Closure

**Files:**
- Modify: `scripts/build_slang_candidate_report.py`
- Test: `tests/test_slang_candidate_report.py`
- Test: `tests/test_slang_candidate_lifecycle_export.py`

- [ ] **Step 1: Write lifecycle export test**

```python
def test_slang_lifecycle_eval_gain_compares_baseline_and_post_reports():
    gain = evaluation_gain_from_reports({"primary_classification_f1": 0.62}, {"primary_classification_f1": 0.67})
    assert gain["primary_classification_f1_delta"] == 0.05
```

- [ ] **Step 2: Verify red if lifecycle evidence is missing**

Run: `pytest tests/test_slang_candidate_lifecycle_export.py::test_slang_lifecycle_eval_gain_compares_baseline_and_post_reports -q`

Expected: missing lifecycle fields fail before implementation, or existing coverage already passes and should be preserved.

- [ ] **Step 3: Implement analyst CSV to runtime-ready lifecycle export**

Keep pending candidates excluded from runtime, promote only approved rows, preserve reviewer/version/batch metadata, and include baseline/post evaluation gain.

- [ ] **Step 4: Verify lifecycle scripts**

Run: `pytest tests/test_slang_candidate_report.py tests/test_slang_candidate_lifecycle_export.py -q`

Expected: candidate report, review CSV, lifecycle records, and evaluation-gain metadata all pass.

### Task 6: End-To-End Defense Demo Evidence

**Files:**
- Modify: `scripts/build_defense_acceptance_report.py`
- Modify: `scripts/serve_demo_api.py` or `scripts/export_acceptance_e2e_evidence.py` only if report evidence lacks source/clean/classify/entity/clue/cost sections.
- Test: `tests/test_defense_acceptance_report.py`
- Test: `tests/test_optimization_boundaries.py`

- [ ] **Step 1: Write or extend acceptance aggregation test**

```python
def test_build_report_aggregates_acceptance_sections_and_test_results(tmp_path):
    report = build_report(collection_stats=..., cleaning_summary=..., classification_summary=..., classifications=..., entities=..., e2e_evidence=..., eval_report=..., test_commands=[...], run_tests=True)
    assert report["collection_coverage"]
    assert report["classification_stats"]["record_review_buckets"]
    assert report["evaluation_metrics"]["primary_classification_f1"] is not None
    assert report["test_results"][0]["returncode"] == 0
```

- [ ] **Step 2: Verify red if report lacks required sections**

Run: `pytest tests/test_defense_acceptance_report.py::test_build_report_aggregates_acceptance_sections_and_test_results -q`

Expected: missing report sections fail before implementation, or existing coverage already passes and should be preserved.

- [ ] **Step 3: Implement one-shot report aggregation**

Aggregate source selection, collection coverage, cleaning stats, classification review buckets, entity counts, clue samples, evaluation metrics, and explicit verification command results.

- [ ] **Step 4: Verify acceptance evidence**

Run: `pytest tests/test_defense_acceptance_report.py tests/test_optimization_boundaries.py -q`

Expected: report gives a runnable defense script artifact and does not overclaim live collection unless live artifacts are referenced.

### Final Verification

- [ ] Run targeted optimization tests:

```bash
pytest tests/test_optimization_boundaries.py tests/test_evaluate_pipeline.py tests/test_slang_candidate_lifecycle_export.py tests/test_defense_acceptance_report.py -q
```

- [ ] Run full non-network suite:

```bash
pytest -q
```

- [ ] Run current acceptance/evaluation scripts needed for evidence:

```bash
python scripts/evaluate_pipeline.py --gold tests/evaluation/gold_classification.jsonl --entities-gold tests/evaluation/gold_entities.jsonl --clues-gold tests/evaluation/gold_clues.jsonl --hard-negative tests/evaluation/hard_negative.jsonl --profile fast --llm-mode off --with-budget --min-primary-classification-f1 0.8 --min-secondary-classification-f1 0.65 --min-hierarchical-classification-f1 0.75 --min-entity-f1 0.8 --max-hard-negative-fpr 0.3 --output data/eval_report_goal_audit.json
python scripts/build_defense_acceptance_report.py --eval-report data/eval_report_goal_audit.json --test-command "pytest tests/test_optimization_boundaries.py tests/test_evaluate_pipeline.py tests/test_slang_candidate_lifecycle_export.py tests/test_defense_acceptance_report.py -q" --run-tests --output data/defense_acceptance_report.json
```

Expected: commands exit 0, and every item in `优化.md` has direct code/test/report evidence.
