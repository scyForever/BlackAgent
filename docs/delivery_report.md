# 交付汇报（当前快照）

本文档汇总当前 `data/collection_phase_delivery.db` 及其派生产物。旧版报告中的历史局部 run 数字不再作为交付口径，本版以当前已复跑产物为准。

## 1. 数据与来源

当前采集底库：

| 指标 | 数值 |
| --- | ---: |
| raw 记录 | 4163 |
| cleaned 记录 | 3464 |
| source 数 | 83 |
| source access type | public_compliant |

全量 raw 来源结构仍然 IM / 群组占主导：

| source_class | 条数 |
| --- | ---: |
| `im_or_group` | 3786 |
| `social_or_forum` | 356 |
| `vertical_or_technical` | 21 |

为答辩和抽检额外交付严格均衡样本 `data/collection_phase_defense_quota_balanced_sample.jsonl`。该样本是答辩/防御评估视图，不代表全量 raw 的天然来源分布：

| source_class | 样本条数 |
| --- | ---: |
| `im_or_group` | 94 |
| `social_or_forum` | 94 |
| `vertical_or_technical` | 21 |

严格样本总数 `209`，manifest warnings 为 `[]`。

## 2. 清洗结果

当前 `data/cleaning_phase_summary.json`：

| 指标 | 数值 |
| --- | ---: |
| 输入 raw | 4163 |
| cleaned | 3464 |
| dropped | 699 |
| high risk | 1095 |
| duplicate dropped | 650 |
| average quality score | 0.7568 |
| average risk score | 0.422 |

对应产物：

- `data/cleaning_phase_cleaned_corpus.jsonl`：3464 行
- `data/cleaning_phase_high_risk_corpus.jsonl`：1095 行

## 3. 分类与抽取

### 3.1 全量 cleaned 视图

当前 `data/classification_extraction_phase_summary.json`：

| 指标 | 数值 |
| --- | ---: |
| 分类输入 | 3464 |
| 分类完成 | 3464 |
| 实体抽取 | 21774 |
| 需人工复核 | 970 |

一级分类：

| 一级分类 | 条数 |
| --- | ---: |
| 正常业务白噪声 | 1744 |
| 账号交易 | 513 |
| 工具交易 | 333 |
| 众包服务 | 304 |
| 诈骗引流 | 302 |
| unknown | 190 |
| 刷单作弊 | 78 |

二级标签仍有 `待研判=398`、`低相关=1737`、`未细分=30`。因此当前可以说“明显公开/技术类白噪声已大幅归入正常业务白噪声”，但不能说全量未知已清零。

### 3.2 高危高质量视图

当前 `data/classification_extraction_phase_high_risk_summary.json`：

| 指标 | 数值 |
| --- | ---: |
| 输入 | 1061 |
| 分类完成 | 1061 |
| 实体抽取 | 4698 |
| 需人工复核 | 461 |

一级分类：

| 一级分类 | 条数 |
| --- | ---: |
| 账号交易 | 360 |
| 工具交易 | 270 |
| 众包服务 | 240 |
| 诈骗引流 | 117 |
| 刷单作弊 | 46 |
| 正常业务白噪声 | 28 |

高危高质量视图里没有一级 `unknown`；但二级标签仍有 `待研判=90`、`未细分=19`。

## 4. 增强项落地

已落地：

- `storage/entity_graph.py` 的时间窗口查询支持注入 `now`，默认 pytest 不再受当前日期影响。
- 分类器增加普通公开资料 / 技术资料白噪声兜底，同时保留交易、联系方式、弱风险样本的复核边界。
- `scripts/export_delivery_corpora.py` 支持严格来源均衡样本，避免评估样本被单一 Telegram 来源主导。
- OCR 主链保留 `content_modality`、`ocr_text`、`ocr_confidence`、`ocr_engine_confidences`、`ocr_confidence_details`。
- `scripts/build_slang_candidate_report.py` 从 pending 样本生成黑话候选报告，候选需人工确认后才能激活。
- `src/enhancement/clue_quality.py` 的线索质量评估加入新鲜度和误报风险，不再只看分类 F1。
- 人工 held-out 已完成 193 条 confirmed/corrected gold，7 条 rejected，并通过 `validate_manual_heldout.py --min-records 100`。

## 5. 评测与边界

OCR hard set：

| 指标 | 数值 |
| --- | ---: |
| 记录数 | 20 |
| primary classification F1 | 1.0 |
| secondary classification F1 | 0.8 |
| hierarchical classification F1 | 0.8 |
| entity F1 | 0.9677 |

人工 held-out：

| 指标 | 数值 |
| --- | ---: |
| seeded review rows | 200 |
| review task rows | 200 |
| min target confirmed rows | 100 |
| confirmed manual gold rows | 193 |
| rejected rows | 7 |
| claim status | human_confirmed_gold_ready |
| primary classification F1 | 0.7484 |
| secondary classification F1 | 0.6124 |
| hierarchical classification F1 | 0.5314 |
| entity F1 | 0.9484 |
| clue F1 | 0.1538 |
| clue recall | 0.0833 |
| object clue F1 | 0.0769 |
| evidence reviewability rate | 1.0 |
| false positive rate | 0.3361 |
| classification review rate | 0.3575 |

黑话候选：

| 指标 | 数值 |
| --- | ---: |
| pending 输入 | 970 |
| candidate count | 80 |
| min count | 3 |

当前不能声称：

- 全量 cleaned 语料 `unknown / 待研判 / 未细分` 已清零。
- 线索召回已经充分达标；当前对象级线索和证据链可复核率已从 0 拉起，但召回仍低，线索产出仍应作为人工复核增强候选。
- 人工 held-out 代表线上生产泛化；当前只证明本地公开 / 授权 held-out split。
- OCR hard set 代表外部生产 OCR 泛化质量。
- 黑话候选已经自动进入正式词库；当前需先在 `data/manual_review/slang_candidate_review_template.csv` 记录人工确认，再进入 review/gray_rollout/activate 生命周期。

## 6. 主要复跑命令

```powershell
python scripts/export_delivery_corpora.py --db data/collection_phase_delivery.db --raw-jsonl-out data/collection_phase_raw_dataset.jsonl --quota-jsonl-out data/collection_phase_quota_balanced_sample.jsonl --defense-quota-jsonl-out data/collection_phase_defense_quota_balanced_sample.jsonl --manifest-out data/collection_phase_delivery_manifest.json

python scripts/run_cleaning_phase.py --db data/collection_phase_delivery.db --summary-out data/cleaning_phase_summary.json --cleaned-jsonl data/cleaning_phase_cleaned_corpus.jsonl --high-risk-jsonl data/cleaning_phase_high_risk_corpus.jsonl --persist-cleaned

python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --source cleaned --summary-out data/classification_extraction_phase_summary.json --classifications-jsonl data/classification_extraction_phase_classifications.jsonl --entities-jsonl data/classification_extraction_phase_entities.jsonl

python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --source cleaned --high-risk-only --min-quality-score 0.7 --summary-out data/classification_extraction_phase_high_risk_summary.json --classifications-jsonl data/classification_extraction_phase_high_risk_classifications.jsonl --entities-jsonl data/classification_extraction_phase_high_risk_entities.jsonl

python scripts/build_slang_candidate_report.py --records data/cleaning_phase_cleaned_corpus.jsonl --classifications data/classification_extraction_phase_classifications.jsonl --output data/slang_candidate_report.json --min-count 3 --max-candidates 80
```
