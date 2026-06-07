# 数据采集阶段交付说明（最终刷新到稳定底库）

本文档对应当前底库 `data/collection_phase_delivery.db` 在 **2026-05-27** 的最终导出口径。  
本轮已基于稳定后的 SQLite 底库重新执行导出与统计刷新，当前交付物可直接用于“原始情报数据集 + 智能清洗前底库”验收。

## 1. 最终交付结论

- 原始情报总量：`4163`
- 已满足“原始情报数据集不少于 4000 条”的交付要求
- 可直接交付原始 JSONL：`data/collection_phase_raw_dataset.jsonl`
- 对应 manifest：`data/collection_phase_delivery_manifest.json`
- 对应统计文件：`data/collection_phase_delivery_stats.json`

## 2. 当前底库统计

### 2.1 原始记录与主题覆盖

- 原始记录：`4163`
- 未命中六类主题但保留在底库中的记录：`2299`
- 主题计数口径：`matched_themes` 多标签成员计数
- relevance 已刷新并写回：`true`

### 2.2 分主题计数

| 主题 | 条数 |
| --- | ---: |
| 众包任务 | 635 |
| 接码 | 598 |
| 账号交易 | 500 |
| 诈骗引流 | 379 |
| 工具交易 | 364 |
| 刷单作弊 | 109 |

### 2.3 查询词扩展覆盖

本轮已把“核心词 + 变种词”同时带入底库，并在最终 raw JSONL 中保留 `query_term_stage`：

| query_term_stage | 条数 |
| --- | ---: |
| core | 384 |
| variant | 47 |

说明：
- `variant` 记录来自当时的专项变种召回实验 catalog；该实验配置现已回收出仓库，避免与当前主链配置混淆
- 当前导出中，`variant` 记录已经固化在最终交付原始数据集里，不依赖仓库继续保留历史实验 YAML

## 3. 特殊信号覆盖（本轮重点验收项）

最终 raw JSONL 已显式标注 `special_signal_types`，用于证明变种 / 谐音 / 表情 / 图片文字已进入交付物。

| 特殊信号 | 条数 |
| --- | ---: |
| `variant_or_homophone_normalized` | 208 |
| `emoji_marker` | 186 |
| `multimodal_text` | 29 |

其中与图片/媒体文本相关的结构化覆盖为：

- `rows_with_attachments`：`29`
- `has_media=true`：`29`
- `message_text_source=photo_caption`：`29`

主要 multimodal 来源：

| source_name | 条数 |
| --- | ---: |
| `telegram_public_delivery:haoshango00` | 12 |
| `telegram_public_delivery:heimayunkong1` | 10 |
| `telegram_public_delivery:TGtelegram101` | 7 |

这意味着：
- **图片相关文本**不再只停留在网页截图描述，而是已通过 `attachments.caption` 物化进导出语料
- 最终 raw JSONL 中可直接按 `attachments`、`has_media`、`message_text_source`、`multimodal_text_sources` 回看图片文本来源

## 4. 主要来源分布

当前底库 top sources（按原始记录数）：

| source_name | 条数 |
| --- | ---: |
| `telegram_public_delivery:Automationforum` | 2059 |
| `x_blackgray_search` | 126 |
| `telegram_public_delivery:TGtelegram101` | 88 |
| `telegram_public_delivery:alanghome` | 84 |
| `telegram_public_delivery:feijisu` | 76 |
| `telegram_public_delivery:tgheji66` | 70 |
| `tech_forum_blackgray_search` | 65 |
| `tieba_blackgray_search` | 64 |

## 5. 来源结构与答辩样本

当前全量 raw 底库仍然是 IM / 群组来源占主导，这一点不能描述成天然均衡：

| source_class | 全量 raw 条数 |
| --- | ---: |
| `im_or_group` | 3786 |
| `social_or_forum` | 356 |
| `vertical_or_technical` | 21 |

为避免答辩、抽检或防御评估被单一 Telegram 公开频道主导，`scripts/export_delivery_corpora.py` 额外导出严格均衡样本：

- `data/collection_phase_defense_quota_balanced_sample.jsonl`
- manifest 字段：`defense_quota_balanced_sample`
- 样本总数：`209`
- class 分布：`im_or_group=94`、`social_or_forum=94`、`vertical_or_technical=21`
- warnings：`[]`

该严格样本用于答辩、抽检和防御评估；全量 raw 仍保留真实采集分布。

## 6. 最终交付文件

### 主数据

- `data/collection_phase_delivery.db`
- `data/collection_phase_raw_dataset.jsonl`
- `data/collection_phase_delivery_manifest.json`
- `data/collection_phase_defense_quota_balanced_sample.jsonl`

### 统计结果

- `data/collection_phase_delivery_stats.json`
- `data/collection_phase_theme_counts.csv`
- `data/collection_phase_source_counts.csv`
- `data/collection_phase_theme_source_counts.csv`

### 采集摘要 / 补充结果

- `data/collection_phase_delivery_telegram_summary.json`
- `data/collection_phase_delivery_telegram_media_focus_summary.json`

## 7. 验证结果

最终重新导出后的产物级核验结果：

- `data/collection_phase_raw_dataset.jsonl`：`4163` 行
- manifest `raw_record_count`：`4163`
- manifest `query_term_stage_counts.variant`：`47`
- manifest `special_signal_counts.multimodal_text`：`29`
- manifest `defense_quota_balanced_sample.selected_count`：`209`
- stats `total_raw_records`：`4163`

上述数字已与当前稳定底库重新对齐，不再使用此前 `4042/4141` 的旧快照。

## 8. 复跑命令

### 刷新 relevance 统计

```powershell
python scripts/export_collection_phase_stats.py --db data/collection_phase_delivery.db --json-out data/collection_phase_delivery_stats.json --theme-csv data/collection_phase_theme_counts.csv --source-csv data/collection_phase_source_counts.csv --cross-csv data/collection_phase_theme_source_counts.csv --refresh-relevance --write-back
```

### 导出最终原始语料 JSONL + signal manifest

```powershell
python scripts/export_delivery_corpora.py --db data/collection_phase_delivery.db --raw-jsonl-out data/collection_phase_raw_dataset.jsonl --quota-jsonl-out data/collection_phase_quota_balanced_sample.jsonl --defense-quota-jsonl-out data/collection_phase_defense_quota_balanced_sample.jsonl --manifest-out data/collection_phase_delivery_manifest.json
```

### 如需补采当前主链数据

当前仓库已删除历史实验/轮次配置，只保留主链配置。若要继续补采，请从主 catalog 和主 Telegram 页面配置出发：

```powershell
python scripts/collect_public_sources.py --catalog config/intel_sources.blackgray.yaml --db data/collection_phase_delivery.db --timeout-seconds 15 --max-records 10 --rate-limit-per-minute 0 --retry-attempts 1 --retry-backoff-seconds 1 --retry-backoff-multiplier 2
python scripts/hydrate_public_search_results.py --db data/collection_phase_delivery.db --only-unhydrated --limit 30 --timeout-seconds 20 --rate-limit-per-minute 0 --retry-attempts 1 --retry-backoff-seconds 1 --retry-backoff-multiplier 2
python scripts/collect_telegram_public_delivery.py --channels-config config/telegram_public_delivery_channels.yaml --db data/collection_phase_delivery.db --summary-path data/collection_phase_delivery_telegram_summary.json --min-records 800 --timeout-seconds 20 --sleep-seconds 0.1
```
