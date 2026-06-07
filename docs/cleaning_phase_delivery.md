# 智能清洗阶段交付说明（最终刷新到稳定底库）

本文档对应 `data/collection_phase_delivery.db` 在 **2026-05-27** 的最终清洗结果。  
本轮已在稳定后的 `4163` 条原始记录上重新执行 cleaning phase，并同步刷新：

- `data/cleaning_phase_summary.json`
- `data/cleaning_phase_cleaned_corpus.jsonl`
- `data/cleaning_phase_high_risk_corpus.jsonl`
- `cleaned_texts` SQL 表

## 1. 已交付能力

- **自动去重**：对重复文本生成 `dedup_group_id`，默认丢弃重复副本
- **低价值噪声过滤**：过滤泛教程、泛推荐、防御语境等非目标情报
- **高危预判打标**：输出 `risk_level / risk_score / risk_categories / risk_markers / quality_score`
- **上下文保留**：清洗后语料保留 `source_name / source_type / matched_themes / query_term / query_term_stage` 等采集上下文
- **multimodal 文本物化**：当 `attachments.caption` 等附加文本存在时，先物化进 `content_text` 再进入清洗链路

## 2. 最终清洗结果

| 指标 | 数值 |
| --- | ---: |
| 输入原始记录 | 4163 |
| 清洗后高质量语料 | 3464 |
| 丢弃总数 | 699 |
| 高危语料 | 1095 |
| 去重丢弃 | 650 |
| 去重组数量 | 3464 |
| 平均质量分 | 0.7568 |
| 平均风险分 | 0.422 |

对应 JSONL 产物行数已核验一致：

- `data/cleaning_phase_cleaned_corpus.jsonl`：`3464` 行
- `data/cleaning_phase_high_risk_corpus.jsonl`：`1095` 行

## 3. 丢弃原因分布

| reason | 条数 |
| --- | ---: |
| `duplicate` | 650 |
| `generic_guide_noise` | 43 |
| `defensive_context_noise` | 6 |

## 4. 风险等级分布

| risk_level | 条数 |
| --- | ---: |
| LOW | 1903 |
| CRITICAL | 921 |
| MEDIUM | 327 |
| HIGH | 174 |
| NONE | 139 |

其中高危子集定义为 `risk_level in {HIGH, CRITICAL}`，当前共 `1095` 条。

## 5. 风险类别分布

| risk_category | 条数 |
| --- | ---: |
| 工具交易 | 725 |
| 群控脚本 | 669 |
| 账号交易 | 660 |
| 代投服务 | 659 |
| 众包服务 | 658 |
| 接码注册 | 431 |
| 诈骗引流 | 333 |
| 拉群获客 | 302 |
| 订单卡单 | 254 |
| 私域导流 | 232 |

## 6. 高风险 marker 快照

当前出现频次最高的 marker 包括：

- `destination_url`：2210
- `众包任务`：510
- `接码`：419
- `诈骗引流`：333
- `群发`：324
- `账号交易`：321
- `工具交易`：272
- `更新`：220
- `功能`：198
- `客户`：194

## 7. 图片/媒体文本在清洗阶段的覆盖

本轮清洗前已先对 raw rows 执行 multimodal materialization，因此图片说明文字不会在进入清洗时丢失。

当前清洗摘要中的 multimodal 覆盖：

| 指标 | 数值 |
| --- | ---: |
| `multimodal_materialized_count` | 29 |
| `multimodal_source_counts.attachments.caption` | 29 |

这说明：

- 至少 `29` 条记录是依赖 `attachments.caption` 补全文本后进入清洗链路
- 图片相关文字情报已进入最终 cleaned corpus，而不是只停留在原始附件字段里

## 8. 运行方式

```powershell
python scripts/run_cleaning_phase.py `
  --db data/collection_phase_delivery.db `
  --summary-out data/cleaning_phase_summary.json `
  --cleaned-jsonl data/cleaning_phase_cleaned_corpus.jsonl `
  --high-risk-jsonl data/cleaning_phase_high_risk_corpus.jsonl `
  --persist-cleaned
```

## 9. 当前边界

- 清洗阶段的 `risk_level / risk_score` 仍然是**预判信号**，不替代后续正式分类 / 抽取结果
- 防御语境和泛教程噪声默认不进入高质量语料
- 当前清洗结果保留了 `query_term_stage`、multimodal 来源以及采集上下文，便于后续继续验证“变种词 / 谐音 / 表情 / 图片文字”是否真正触达最终语料
