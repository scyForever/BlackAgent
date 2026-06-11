#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 Agent.docx 分阶段目标重排并精简交付数据包。

把 `交付文件/delivery_data/` 重组成与课题文档「分阶段目标」一一对应的目录：

    00_总览与验收           —— 验收摘要 + 人工 held-out / 线索 / 规模 / OCR 评测证据
    01_数据采集_原始情报数据集     —— 阶段一产出物：原始情报数据集
    02_智能清洗_清洗后高质量语料   —— 阶段二产出物：清洗后高质量语料
    03_意图分类_风险分类与标签体系 —— 阶段三产出物：分类结果 + 标签体系(risk_taxonomy)
    04_实体抽取_结构化实体库       —— 阶段四产出物：结构化实体库
    05_风险线索与证据链           —— 总体愿景产出：风险线索 / 风险样本 + 可追溯证据链

核心动作：把「全量生产 run」与「final3 答辩 run」两份真实数据**合并到一起**。
两个 run 的 trace_id / hash_id / source_url 完全不相交（overlap=0），因此合并是
无损并集；每条记录加一个 `delivery_source_run` 溯源字段以便区分来源 run。

数据从仓库内的权威源（data/ · config/ · tests/evaluation/）读取，可复跑、幂等。
精简：删去 raw 级分类/实体中间件、证据输入包、授权源复跑包等冗余变体（仍在 git 历史与 data/ 中可查）。

复跑：  D:/Anaconda/python.exe scripts/build_phase_delivery_package.py
"""
from __future__ import annotations

import json
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CONFIG = os.path.join(ROOT, "config")
TESTS = os.path.join(ROOT, "tests", "evaluation")
PKG = os.path.join(ROOT, "交付文件", "delivery_data")

DIR_OVERVIEW = "00_总览与验收"
DIR_COLLECT = "01_数据采集_原始情报数据集"
DIR_CLEAN = "02_智能清洗_清洗后高质量语料"
DIR_CLASSIFY = "03_意图分类_风险分类与标签体系"
DIR_ENTITY = "04_实体抽取_结构化实体库"
DIR_CLUE = "05_风险线索与证据链"

NEW_DIRS = [DIR_OVERVIEW, DIR_COLLECT, DIR_CLEAN, DIR_CLASSIFY, DIR_ENTITY, DIR_CLUE]
# 旧的 ad-hoc 目录（重排后删除其残留）
OLD_DIRS = [
    "00_summary",
    "01_final3_collection",
    "02_evidence_pack",
    "03_manual_eval",
    "04_stage_corpora",
    "05_model_ocr_benchmark",
]

FULL_RUN = "full_production_run"
FINAL3_RUN = "final3_defense_run"


# --------------------------------------------------------------------------- io
def read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def data_path(*names):
    """返回首个存在的候选路径（用于 .md 附录这类只在 delivery_data 内的输入）。"""
    for name in names:
        if os.path.isabs(name) and os.path.exists(name):
            return name
    for name in names:
        cand = os.path.join(DATA, name)
        if os.path.exists(cand):
            return cand
    return os.path.join(DATA, names[0])


# ----------------------------------------------------------------------- merge
def merge_runs(full_src, final3_src, out_rel, report):
    """并集合并全量 run 与 final3 run，逐条打 delivery_source_run 溯源。"""
    full_rows = read_jsonl(os.path.join(DATA, full_src))
    final3_rows = read_jsonl(os.path.join(DATA, final3_src))
    for row in full_rows:
        row["delivery_source_run"] = FULL_RUN
    for row in final3_rows:
        row["delivery_source_run"] = FINAL3_RUN
    merged = full_rows + final3_rows
    out_abs = os.path.join(PKG, out_rel)
    write_jsonl(out_abs, merged)
    report["merged"].append(
        {
            "file": out_rel,
            "full_rows": len(full_rows),
            "final3_rows": len(final3_rows),
            "total_rows": len(merged),
        }
    )
    return len(merged)


def copy_file(src_abs, out_rel, report, note=""):
    out_abs = os.path.join(PKG, out_rel)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    shutil.copy2(src_abs, out_abs)
    report["copied"].append({"file": out_rel, "from": os.path.relpath(src_abs, ROOT), "note": note})


def copy_tree(src_abs, out_rel, report, note=""):
    out_abs = os.path.join(PKG, out_rel)
    if os.path.isdir(out_abs):
        shutil.rmtree(out_abs)
    shutil.copytree(src_abs, out_abs)
    n = len([f for f in os.listdir(out_abs) if os.path.isfile(os.path.join(out_abs, f))])
    report["copied"].append({"file": out_rel + "/", "from": os.path.relpath(src_abs, ROOT), "note": f"{note} ({n} files)"})


# --------------------------------------------------------------------- cleanup
def cleanup_old(report):
    for old in OLD_DIRS:
        path = os.path.join(PKG, old)
        if os.path.isdir(path):
            shutil.rmtree(path)
            report["removed_dirs"].append(old)
    # 旧的 .md 附录在 delivery_data 根（已复制进 00_ 后删除根副本）
    for md in ("BlackAgent_原始数据完整内容.md", "BlackAgent_真实样例逐步明细.md"):
        root_copy = os.path.join(PKG, md)
        if os.path.exists(root_copy):
            os.remove(root_copy)
            report["removed_root_md"].append(md)


# ---------------------------------------------------------------------- README
def write_readme(report):
    counts = {m["file"].split("/")[-1]: m for m in report["merged"]}

    def total(name):
        return counts[name]["total_rows"]

    def full(name):
        return counts[name]["full_rows"]

    def fin(name):
        return counts[name]["final3_rows"]

    text = f"""# BlackAgent 交付数据包（按分阶段目标组织）

本目录按课题 `Agent.docx` 的「分阶段目标」组织，每个阶段目录直接对应该阶段的**产出物**；
另含一份验收总览和一份风险线索/证据链（对应课题总体愿景）。

> 数据由 `scripts/build_phase_delivery_package.py` 从仓库权威源（`data/` · `config/` · `tests/evaluation/`）生成，可复跑。
> 边界：所有线索均为**人工复核候选**，不是执法定性或自动处置；指标来自本地公开/授权 held-out，不代表线上泛化。

## 全量 run 与 final3 run 已合并

各阶段语料把两份**真实** run 合并到一起（二者 trace_id / 来源 URL 完全不相交，是无损并集）：

- `full_production_run`：全量生产 run（83 来源，规模化采集）。
- `final3_defense_run`：final3 答辩 run（精选可读切片，命中互补来源）。

每条记录带 `delivery_source_run` 字段标明来源 run，便于核对与按 run 过滤。

## 目录与产出物

| 目录 | 对应阶段目标 | 产出物 | 主要文件 |
| --- | --- | --- | --- |
| `00_总览与验收/` | （横向）验收与评测 | 最终验收摘要 + 人工评测证据 | `final_acceptance_summary.json`、人工 held-out 分类/实体评测、线索召回评测、LLM 价值/规模/OCR 报告、逐步明细附录 |
| `01_数据采集_原始情报数据集/` | 数据采集：打通 IM/群组/论坛等≥3 类源 | 原始情报数据集 | `raw_dataset.jsonl`（{total('raw_dataset.jsonl')} 行 = 全量 {full('raw_dataset.jsonl')} + final3 {fin('raw_dataset.jsonl')}）、`hydrated_pages.jsonl`、`external_balanced_source_evidence_pack.jsonl`（四类来源均衡） |
| `02_智能清洗_清洗后高质量语料/` | 智能清洗：去重/过滤噪声/识别高危 | 清洗后高质量语料 | `cleaned_corpus.jsonl`（{total('cleaned_corpus.jsonl')} 行）、`high_risk_corpus.jsonl`（{total('high_risk_corpus.jsonl')} 行） |
| `03_意图分类_风险分类与标签体系/` | 意图分类：按风险类型自动分类 | 风险分类结果 + 标签体系 | `classifications.jsonl`（{total('classifications.jsonl')} 行）、`risk_taxonomy.yaml`（标签体系） |
| `04_实体抽取_结构化实体库/` | 实体抽取：黑话/链接/账号/工具等 | 结构化实体库 | `entities.jsonl`（{total('entities.jsonl')} 条实体） |
| `05_风险线索与证据链/` | （愿景）情报→可复核线索/样本 | 风险线索 + 证据链 | 500 行 joined evidence pack、线索证据索引（4 线索/17 证据卡）、精选线索、156 份来源 snapshot |

## 核心数字（合并后交付口径）

| 阶段 | 数据 | 行/条数 |
| --- | --- | ---: |
| 采集 | 原始情报数据集（全量∪final3） | {total('raw_dataset.jsonl')} |
| 清洗 | 清洗后语料 / 高风险子集 | {total('cleaned_corpus.jsonl')} / {total('high_risk_corpus.jsonl')} |
| 分类 | 分类结果 | {total('classifications.jsonl')} |
| 抽取 | 结构化实体 | {total('entities.jsonl')} |

> 分类/实体/线索的质量指标在各自评测集上测得（人工 held-out 193 条：一级分类 F1 0.8662、实体 F1 0.9484；
> 线索 gold 24 条召回/精确/F1 1.0；门控基于全量生产 run）。合并交付的是**数据集本身**，指标口径见 `00_总览与验收/`。

## 已精简（移出主交付，仍在 git 历史与 `data/` 可查）

- raw 级分类 / 实体中间件（`acceptance_direct_final3_raw_classifications/entities.jsonl`）—— 已被 cleaned 级权威结果取代。
- 证据输入包 `collection_phase_multi_source_acceptance_pack.jsonl` —— 已被 500 行 joined evidence pack 取代。
- 授权源复跑包 `authorized_source_rerun_pack.jsonl` —— 外部真实来源已由均衡证据包 + snapshot 覆盖。
- final3 旧 manifest —— 由本目录 `delivery_manifest.json` 取代。

机器可读清单见 `delivery_manifest.json`。
"""
    out = os.path.join(PKG, "README.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    report["written"].append("README.md")


def write_manifest(report):
    def listing(rel_dir):
        abs_dir = os.path.join(PKG, rel_dir)
        items = []
        for name in sorted(os.listdir(abs_dir)):
            p = os.path.join(abs_dir, name)
            if os.path.isfile(p):
                items.append({"name": name, "bytes": os.path.getsize(p)})
            elif os.path.isdir(p):
                nfiles = len([f for f in os.listdir(p) if os.path.isfile(os.path.join(p, f))])
                items.append({"name": name + "/", "files": nfiles})
        return items

    manifest = {
        "package": "BlackAgent delivery_data",
        "organized_by": "Agent.docx 分阶段目标",
        "generator": "scripts/build_phase_delivery_package.py",
        "merge_note": "各阶段语料 = full_production_run ∪ final3_defense_run（两 run 不相交，无损并集；每条记录带 delivery_source_run）",
        "merged_corpora": report["merged"],
        "structure": {d: listing(d) for d in NEW_DIRS},
        "trimmed_as_redundant": [
            "acceptance_direct_final3_raw_classifications.jsonl",
            "acceptance_direct_final3_raw_entities.jsonl",
            "acceptance_direct_final3_delivery_manifest.json",
            "collection_phase_multi_source_acceptance_pack.jsonl",
            "authorized_source_rerun_pack.jsonl",
        ],
        "boundaries": [
            "线索均为人工复核候选，非执法定性、非自动处置",
            "指标来自本地公开/授权 held-out，不代表线上泛化",
            "不覆盖私群/登录后页面/验证码绕过/购买数据",
        ],
    }
    out = os.path.join(PKG, "delivery_manifest.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    report["written"].append("delivery_manifest.json")


# ------------------------------------------------------------------------ main
def main():
    report = {"merged": [], "copied": [], "written": [], "removed_dirs": [], "removed_root_md": []}
    for d in NEW_DIRS:
        os.makedirs(os.path.join(PKG, d), exist_ok=True)

    # ---- 01 数据采集：原始情报数据集（合并）
    merge_runs("collection_phase_raw_dataset.jsonl",
               "acceptance_direct_final3_raw_dataset.jsonl",
               f"{DIR_COLLECT}/raw_dataset.jsonl", report)
    copy_file(os.path.join(DATA, "acceptance_direct_final3_hydrated_pages.jsonl"),
              f"{DIR_COLLECT}/hydrated_pages.jsonl", report, "final3 hydrated 网页正文")
    copy_file(os.path.join(DATA, "external_balanced_source_evidence_pack.jsonl"),
              f"{DIR_COLLECT}/external_balanced_source_evidence_pack.jsonl", report, "四类来源均衡证据包")

    # ---- 02 智能清洗：清洗后高质量语料（合并）
    merge_runs("cleaning_phase_cleaned_corpus.jsonl",
               "acceptance_direct_final3_cleaned_corpus.jsonl",
               f"{DIR_CLEAN}/cleaned_corpus.jsonl", report)
    merge_runs("cleaning_phase_high_risk_corpus.jsonl",
               "acceptance_direct_final3_high_risk_corpus.jsonl",
               f"{DIR_CLEAN}/high_risk_corpus.jsonl", report)

    # ---- 03 意图分类：分类结果 + 标签体系（合并 + taxonomy）
    merge_runs("classification_extraction_phase_classifications.jsonl",
               "acceptance_direct_final3_classifications.jsonl",
               f"{DIR_CLASSIFY}/classifications.jsonl", report)
    copy_file(os.path.join(CONFIG, "risk_taxonomy.yaml"),
              f"{DIR_CLASSIFY}/risk_taxonomy.yaml", report, "标签体系定义")

    # ---- 04 实体抽取：结构化实体库（合并）
    merge_runs("classification_extraction_phase_entities.jsonl",
               "acceptance_direct_final3_entities.jsonl",
               f"{DIR_ENTITY}/entities.jsonl", report)

    # ---- 05 风险线索与证据链
    copy_file(os.path.join(DATA, "collection_phase_multi_source_evidence_pack.jsonl"),
              f"{DIR_CLUE}/collection_phase_multi_source_evidence_pack.jsonl", report, "500 行 joined evidence pack")
    copy_file(os.path.join(DATA, "collection_phase_multi_source_evidence_pack_report.json"),
              f"{DIR_CLUE}/collection_phase_multi_source_evidence_pack_report.json", report, "证据包完整性报告")
    copy_file(os.path.join(DATA, "collection_phase_multi_source_clue_evidence_index.json"),
              f"{DIR_CLUE}/collection_phase_multi_source_clue_evidence_index.json", report, "线索证据索引(4线索/17卡)")
    copy_file(os.path.join(DATA, "collection_phase_multi_source_curated_clues.jsonl"),
              f"{DIR_CLUE}/collection_phase_multi_source_curated_clues.jsonl", report, "4 条精选线索")
    copy_tree(os.path.join(DATA, "external_source_evidence_snapshots"),
              f"{DIR_CLUE}/external_source_evidence_snapshots", report, "来源 snapshot / raw payload")

    # ---- 00 总览与验收
    copy_file(os.path.join(DATA, "final_acceptance_summary.json"),
              f"{DIR_OVERVIEW}/final_acceptance_summary.json", report, "最终验收摘要")
    copy_file(os.path.join(DATA, "manual_heldout_eval_current.json"),
              f"{DIR_OVERVIEW}/manual_heldout_eval_current.json", report, "人工 held-out 分类/实体评测")
    copy_file(os.path.join(DATA, "eval_manual_heldout_clue_recall_report.json"),
              f"{DIR_OVERVIEW}/eval_manual_heldout_clue_recall_report.json", report, "人工线索 gold 召回评测")
    copy_file(os.path.join(DATA, "manual_heldout_report.json"),
              f"{DIR_OVERVIEW}/manual_heldout_report.json", report, "人工复核 gold 生成报告")
    copy_file(os.path.join(TESTS, "manual_heldout_classification.jsonl"),
              f"{DIR_OVERVIEW}/manual_heldout_classification.jsonl", report, "193 行人工确认 gold")
    copy_file(os.path.join(TESTS, "manual_heldout_clues.jsonl"),
              f"{DIR_OVERVIEW}/manual_heldout_clues.jsonl", report, "24 条人工线索 gold")
    copy_file(os.path.join(DATA, "manual_review", "heldout_review_task.csv"),
              f"{DIR_OVERVIEW}/heldout_review_task.csv", report, "人工复核过程表")
    copy_file(os.path.join(DATA, "latest_llm_value_report.json"),
              f"{DIR_OVERVIEW}/latest_llm_value_report.json", report, "LLM 价值门控")
    copy_file(os.path.join(DATA, "eval_llm_ablation.json"),
              f"{DIR_OVERVIEW}/eval_llm_ablation.json", report, "LLM 价值/成本消融")
    copy_file(os.path.join(DATA, "eval_llm_hard_ablation.json"),
              f"{DIR_OVERVIEW}/eval_llm_hard_ablation.json", report, "hard cases 消融")
    copy_file(os.path.join(DATA, "scale_benchmark_report.json"),
              f"{DIR_OVERVIEW}/scale_benchmark_report.json", report, "本地规模 benchmark")
    copy_file(os.path.join(DATA, "ocr_hardset_report.json"),
              f"{DIR_OVERVIEW}/ocr_hardset_report.json", report, "OCR hardset 报告")
    # .md 附录：只在 delivery_data 内（旧根目录或已迁入 00_），就近取源
    for md in ("BlackAgent_原始数据完整内容.md", "BlackAgent_真实样例逐步明细.md"):
        src = data_path(os.path.join(PKG, md), os.path.join(PKG, DIR_OVERVIEW, md))
        copy_file(src, f"{DIR_OVERVIEW}/{md}", report, "真实样例 / 原始数据附录")

    cleanup_old(report)
    write_readme(report)
    write_manifest(report)

    # -------------------------------------------------------------- report out
    print("=== 合并语料（产出物） ===")
    for m in report["merged"]:
        print(f"  {m['file']:<46} {m['total_rows']:>6} = full {m['full_rows']} + final3 {m['final3_rows']}")
    print(f"\n=== 复制/迁移文件: {len(report['copied'])} ===")
    for c in report["copied"]:
        print(f"  {c['file']:<58} <- {c['from']}  {c['note']}")
    print(f"\n=== 删除旧目录: {report['removed_dirs']}")
    print(f"=== 删除根 .md 副本: {report['removed_root_md']}")
    print(f"=== 写入: {report['written']}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
