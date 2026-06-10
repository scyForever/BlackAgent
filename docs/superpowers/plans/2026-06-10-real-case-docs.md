# Real Case Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the BlackAgent delivery and acceptance documents easier for judges to understand by adding real, traceable use cases and more direct evaluation-facing wording.

**Architecture:** This is a documentation-only change. A new short use-case document presents four traceable evidence-chain examples, while the two existing delivery documents link those examples back to the final acceptance metrics and compliance boundaries.

**Tech Stack:** Markdown documentation, local JSON/JSONL evidence artifacts, PowerShell verification commands.

---

### Task 1: Add Real Use-Case Overview

**Files:**
- Create: `docs/答辩验收材料/BlackAgent_真实用例速览.md`
- Source data: `data/collection_phase_multi_source_curated_clues.jsonl`
- Source data: `data/collection_phase_multi_source_clue_evidence_index.json`

- [x] **Step 1: Create the overview document**

Add a concise judge-facing Markdown document with four cases:

```text
群控脚本、接码平台、群发/云控、实名号/卖号
```

Each case must show source type, evidence count, quality score, raw public example, system output, extracted entities, evidence-chain reason, and review boundary.

- [x] **Step 2: Keep claims bounded**

Make clear that these are public or authorized evidence-chain examples and are not automatic law-enforcement conclusions or production enforcement results.

### Task 2: Update Delivery Document

**Files:**
- Modify: `docs/交付文档.md`

- [x] **Step 1: Add direct judge-facing summary**

Add a short "评委先看这三件事" block near the top:

```text
真实数据跑通、证据链可追溯、交付边界清楚
```

- [x] **Step 2: Add real-case section**

Add a compact table pointing to the four real use cases and the new overview document.

- [x] **Step 3: Make the defense wording more direct**

Update the defense talk track so it starts from real examples before discussing metrics.

### Task 3: Update Acceptance Report

**Files:**
- Modify: `docs/答辩验收材料/BlackAgent_验收报告.md`

- [x] **Step 1: Add direct conclusion**

Add a one-screen conclusion that tells judges exactly what was built, what data was processed, what was produced, and what cannot be claimed.

- [x] **Step 2: Add real-case overview**

Add a section summarizing the same four cases and link to `BlackAgent_真实用例速览.md`.

### Task 4: Verify Documentation Consistency

**Files:**
- Verify: `docs/交付文档.md`
- Verify: `docs/答辩验收材料/BlackAgent_验收报告.md`
- Verify: `docs/答辩验收材料/BlackAgent_真实用例速览.md`

- [x] **Step 1: Check stale or exaggerated claims**

Run:

```powershell
rg -n "1000 条人工|30-50|80 个正式黑话|生产实时|自动封禁|私群|登录后" docs data/final_acceptance_summary.json
```

Expected: only bounded negative statements or explicit non-claims.

- [x] **Step 2: Check referenced artifacts exist**

Run a PowerShell path check for all newly referenced data and docs paths.

Expected: no missing paths.
