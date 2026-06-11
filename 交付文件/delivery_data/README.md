# BlackAgent 交付数据包（按分阶段目标组织）

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
| `01_数据采集_原始情报数据集/` | 数据采集：打通 IM/群组/论坛等≥3 类源 | 原始情报数据集 | `raw_dataset.jsonl`（4568 行 = 全量 4163 + final3 405）、`hydrated_pages.jsonl`、`external_balanced_source_evidence_pack.jsonl`（四类来源均衡） |
| `02_智能清洗_清洗后高质量语料/` | 智能清洗：去重/过滤噪声/识别高危 | 清洗后高质量语料 | `cleaned_corpus.jsonl`（3732 行）、`high_risk_corpus.jsonl`（1246 行） |
| `03_意图分类_风险分类与标签体系/` | 意图分类：按风险类型自动分类 | 风险分类结果 + 标签体系 | `classifications.jsonl`（3732 行）、`risk_taxonomy.yaml`（标签体系） |
| `04_实体抽取_结构化实体库/` | 实体抽取：黑话/链接/账号/工具等 | 结构化实体库 | `entities.jsonl`（22416 条实体） |
| `05_风险线索与证据链/` | （愿景）情报→可复核线索/样本 | 风险线索 + 证据链 | 500 行 joined evidence pack、线索证据索引（4 线索/17 证据卡）、精选线索、156 份来源 snapshot |

## 核心数字（合并后交付口径）

| 阶段 | 数据 | 行/条数 |
| --- | --- | ---: |
| 采集 | 原始情报数据集（全量∪final3） | 4568 |
| 清洗 | 清洗后语料 / 高风险子集 | 3732 / 1246 |
| 分类 | 分类结果 | 3732 |
| 抽取 | 结构化实体 | 22416 |

> 分类/实体/线索的质量指标在各自评测集上测得（人工 held-out 193 条：一级分类 F1 0.8662、实体 F1 0.9484；
> 线索 gold 24 条召回/精确/F1 1.0；门控基于全量生产 run）。合并交付的是**数据集本身**，指标口径见 `00_总览与验收/`。

## 已精简（移出主交付，仍在 git 历史与 `data/` 可查）

- raw 级分类 / 实体中间件（`acceptance_direct_final3_raw_classifications/entities.jsonl`）—— 已被 cleaned 级权威结果取代。
- 证据输入包 `collection_phase_multi_source_acceptance_pack.jsonl` —— 已被 500 行 joined evidence pack 取代。
- 授权源复跑包 `authorized_source_rerun_pack.jsonl` —— 外部真实来源已由均衡证据包 + snapshot 覆盖。
- final3 旧 manifest —— 由本目录 `delivery_manifest.json` 取代。

机器可读清单见 `delivery_manifest.json`。
