# 分类 / 抽取阶段交付说明

本文档记录当前 `data/collection_phase_delivery.db` 的分类 / 抽取交付口径。数字来自已重新生成的：

- `data/classification_extraction_phase_summary.json`
- `data/classification_extraction_phase_high_risk_summary.json`
- `data/classification_extraction_phase_classifications.jsonl`
- `data/classification_extraction_phase_entities.jsonl`

## 1. 当前产出视图

### 1.1 全量 cleaned 视图

复跑命令：

```powershell
python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --source cleaned --summary-out data/classification_extraction_phase_summary.json --classifications-jsonl data/classification_extraction_phase_classifications.jsonl --entities-jsonl data/classification_extraction_phase_entities.jsonl
```

当前结果：

| 指标 | 数值 |
| --- | ---: |
| raw snapshot | 4163 |
| cleaned snapshot | 3464 |
| 分类输入 | 3464 |
| 分类完成 | 3464 |
| 实体抽取 | 21774 |
| 需人工复核 | 970 |

一级分类计数：

| 一级分类 | 条数 |
| --- | ---: |
| 正常业务白噪声 | 1744 |
| 账号交易 | 513 |
| 工具交易 | 333 |
| 众包服务 | 304 |
| 诈骗引流 | 302 |
| unknown | 190 |
| 刷单作弊 | 78 |

二级标签计数中仍有 `待研判=398`、`低相关=1737`、`未细分=30`。因此当前不能声称全量 cleaned 语料已经把 `unknown / 待研判 / 未细分` 清零。

### 1.2 高危高质量视图

复跑命令：

```powershell
python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --source cleaned --high-risk-only --min-quality-score 0.7 --summary-out data/classification_extraction_phase_high_risk_summary.json --classifications-jsonl data/classification_extraction_phase_high_risk_classifications.jsonl --entities-jsonl data/classification_extraction_phase_high_risk_entities.jsonl
```

当前结果：

| 指标 | 数值 |
| --- | ---: |
| raw snapshot | 4163 |
| cleaned snapshot | 3464 |
| 高危高质量输入 | 1061 |
| 分类完成 | 1061 |
| 实体抽取 | 4698 |
| 需人工复核 | 461 |

一级分类计数：

| 一级分类 | 条数 |
| --- | ---: |
| 账号交易 | 360 |
| 工具交易 | 270 |
| 众包服务 | 240 |
| 诈骗引流 | 117 |
| 刷单作弊 | 46 |
| 正常业务白噪声 | 28 |

高危高质量视图中，一级分类没有 `unknown`。但二级标签仍有 `待研判=90`、`未细分=19`，这些样本继续保留复核边界。

## 2. 本轮优化点

### 2.1 正常业务白噪声兜底

分类器现在会把明显公共资料、技术说明、教程介绍等无交易/联系方式意图的文本归入 `正常业务白噪声 / 低相关`，避免这类样本继续堆在 `unknown`。同时含直接联系、交易邀约、账号/工具/任务风险信号的弱证据文本仍保留 `待研判` 和人工复核。

当前效果：

- 全量 cleaned 中 `正常业务白噪声=1744`
- 高危高质量视图中 `正常业务白噪声=28`
- 全量 cleaned 仍保留 `unknown=190`，不做过度承诺

### 2.2 图片文字与 OCR 信心

多模态入口现在保留：

- `content_modality=image_text/mixed/text`
- `ocr_text`
- `ocr_confidence`
- `ocr_engine_confidences`
- `ocr_confidence_details`

OCR hard set 产物：

| 产物 | 当前结果 |
| --- | ---: |
| `tests/evaluation/ocr_image_text_hardset.jsonl` | 20 行 |
| `content_modality=image_text` | 20 |
| `ocr_status=completed` | 20 |

OCR hard set 评测 `data/eval_ocr_hardset_report.json`：

| 指标 | 数值 |
| --- | ---: |
| primary classification F1 | 1.0 |
| secondary classification F1 | 0.8 |
| hierarchical classification F1 | 0.8 |
| entity F1 | 0.9677 |
| review rate | 0.4 |

边界：该 hard set 验证图片文字合同和确定性 OCR 路径，不代表外部 OCR 引擎在线质量。

### 2.3 黑话候选发现闭环

新增 `scripts/build_slang_candidate_report.py`，从 `unknown / 待研判 / 未细分 / review_required` 样本中挖掘高频上下文 n-gram，并输出待人工确认候选。

当前报告 `data/slang_candidate_report.json`：

| 指标 | 数值 |
| --- | ---: |
| 输入 cleaned 记录 | 3464 |
| pending 分类记录 | 970 |
| 候选数量 | 80 |
| min count | 3 |

高频候选示例包括 `库存`、`协议`、`直登`、`机器人`、`频道`、`采集`、`批量`、`验证`、`小号`、`批发`。这些只是发现线索，`manual_review.claim_boundary` 明确要求人工确认后才能进入动态黑话生命周期，不能直接写入正式词库。人工确认建议使用 `data/manual_review/slang_candidate_review_template.csv` 记录 `approved/rejected/needs_more_evidence`、归一化词、目标风险类、reviewer 和日期，再进入 `DynamicSlangLifecycleManager.review/gray_rollout/activate`。

### 2.4 人工 held-out 评估

当前人工 held-out 已经有人审完并通过验证：

| 产物 | 当前结果 |
| --- | ---: |
| `tests/evaluation/heldout_classification.jsonl` | 200 行 |
| `data/manual_review/heldout_review_task.csv` | 200 行 |
| `data/manual_review/heldout_review_task_report.json` | `ready_for_human_review` |
| `data/manual_heldout_report.json` | `completed` |
| 已确认人工 gold | 193 |
| rejected | 7 |
| 最低人工确认目标 | 100 |
| claim status | `human_confirmed_gold_ready` |

`data/eval_manual_heldout_report.json` 的人工 gold 离线评估：primary F1=0.6259，secondary F1=0.5088，hierarchical F1=0.4216，entity F1=0.9875，false positive rate=0.4454，classification review rate=0.4508。边界：该结果只证明本地公开 / 授权 held-out split，不代表线上生产泛化。

## 3. 实体抽取结果

全量 cleaned 视图实体类型：

| 实体类型 | 条数 |
| --- | ---: |
| url | 12346 |
| contact | 3030 |
| invite_code | 2961 |
| slang_term | 2852 |
| tool_name | 375 |
| settlement | 110 |
| account | 100 |

高危高质量视图实体类型：

| 实体类型 | 条数 |
| --- | ---: |
| url | 2251 |
| contact | 1153 |
| slang_term | 824 |
| tool_name | 303 |
| settlement | 92 |
| invite_code | 71 |
| account | 4 |

## 4. 关键代码入口

- `src/classifier/nlp_rule_matcher.py`
- `src/enhancement/text_intelligence.py`
- `src/enhancement/source_intake.py`
- `src/ocr/image_text.py`
- `scripts/run_classification_extraction_phase.py`
- `scripts/build_slang_candidate_report.py`

## 5. 当前边界

- 全量 cleaned 语料仍有大量 `unknown / 待研判`，这是候选复核池，不是已确认黑灰产事实。
- 高危高质量视图可以说明“一级 unknown 已不存在”，但不能扩展成“全量未知已归零”。
- `正常业务白噪声` 是低相关归档判断，不等于证明页面永久安全。
- 人工 held-out gold 已可用，但只能支撑本地公开 / 授权 held-out split 的离线结论。
