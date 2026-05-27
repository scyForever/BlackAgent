# 分类 / 抽取阶段交付说明

本文档记录 2026-05-26 基于 `data/collection_phase_delivery.db` 的**分类 / 抽取阶段最新版产物**。  
本轮输入不是全量 4042 条原始底库直接硬跑，而是先基于最新 `keyword_relevance_v6` 结果，只取仍带主题标签的 `1755` 条记录进入分类 / 抽取链路。

## 1. 本轮实际产出

- 输入原始底库：`data/collection_phase_delivery.db`
- 进入分类 / 抽取阶段的样本数：`1755`
- 实际完成分类：`1755`
- 实际抽取实体：`4597`
- 需要人工复核：`739`
- 原先 `unknown=759`：**已压到 `0`**
- 后续残留 `secondary_label=未细分`：**`82 -> 0`**

### 一级分类计数

| 一级分类 | 条数 |
| --- | ---: |
| 账号交易 | 688 |
| 工具交易 | 466 |
| 众包服务 | 405 |
| 诈骗引流 | 103 |
| 刷单作弊 | 93 |

### 高频二级标签

| 二级标签 | 条数 |
| --- | ---: |
| 群控脚本 | 461 |
| 接码注册 | 424 |
| 实名账号买卖 | 231 |
| 代投服务 | 221 |
| 拉群获客 | 172 |
| 私域导流 | 74 |
| 刷单返佣 | 33 |
| 账号养号 | 33 |
| 垫付兼职 | 22 |
| 拉群语义 | 19 |
| 手工做单 | 16 |
| 订单卡单 | 14 |
| 打粉卖量 | 12 |
| 卡单玩法 | 8 |

> 本轮已把原先 `拉人获客=469` 继续拆成 `拉群获客 / 打粉卖量 / 代投服务`，并继续吃掉了后续残留的 `未细分=82`；同时，一批明显“软件更新 / 教程 / 下载”文案被重新推回 `工具交易 / 群控脚本`。

### 高频实体类型

| 实体类型 | 条数 |
| --- | ---: |
| contact | 1445 |
| tool_name | 1365 |
| url | 810 |
| slang_term | 605 |
| settlement | 265 |
| account | 107 |

### 高频实体示例

- `tool_name::接码`：`912`
- `slang_term::Telegram`：`352`
- `tool_name::协议号`：`264`
- `slang_term::抖音`：`247`
- `settlement::支付宝`：`157`
- `tool_name::接码平台`：`114`

## 2. 本轮结论

### 2.1 分类 / 抽取链路已经跑通并完成标签升级

本轮已经稳定输出：

- 一级分类
- 二级标签
- `review_required`
- 联系方式 / 工具名 / URL / 黑话 / 结算方式 / 账号 等实体

交付产物已落成到文件：

- `data/classification_extraction_phase_summary.json`
- `data/classification_extraction_phase_classifications.jsonl`
- `data/classification_extraction_phase_entities.jsonl`

### 2.2 这轮的核心成果不是“多跑一遍”，而是把 unknown / 未细分 / 粗粒度服务桶一起压下去

上一轮分类结果里：

- `unknown = 759`

本轮补完分类规则后：

- `unknown = 0`
- `secondary_label=未细分 = 0`

新增和增强的主要分类能力包括：

- 一级类：`众包服务`
- 服务细分：`拉群获客` / `打粉卖量` / `代投服务` / `代运营`
- 残留样本承接标签：`拉群语义` / `打粉引流` / `订单卡单` / `卡单玩法` / `手工做单`
- 旧类增强：`接码注册` / `群控脚本` / `私域导流`

### 2.3 当前主要聚集源已经从 unknown / 未细分 迁移到明确类别

例如：

- `telegram_public_delivery:TGtelegram101` / `huzige1916` -> 以 `拉群获客` 为主
- `telegram_public_delivery:alanghome` / `tgzs88` / `LYZDH` -> 以 `代投服务` 为主
- `telegram_public_delivery:kuajing003` -> 开始出现 `打粉卖量`
- `telegram_public_delivery:HHweb_yk` / `heimayunkong1` -> 以 `工具交易 / 群控脚本` 为主
- `tieba_task_fraud_search` -> 由原先粗粒度 `未细分` 收敛到 `订单卡单 / 卡单玩法 / 手工做单`
- `telegram_public_delivery:alanghome` / `tgchqf` / `haodi00` -> 一部分原本挂在服务桶里的工具更新文案，已回流到 `工具交易`

这说明当前规则已经把之前“看得见主题、分不出类别”的服务型文本压进了明确分类桶。

## 3. 复跑命令

```powershell
python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --summary-out data/classification_extraction_phase_summary.json --classifications-jsonl data/classification_extraction_phase_classifications.jsonl --entities-jsonl data/classification_extraction_phase_entities.jsonl --only-labeled
```

如果前面已经跑过清洗阶段并持久化了 `cleaned_texts`，现在可以直接让分类 / 抽取阶段消费清洗结果：

```powershell
python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --source cleaned --summary-out data/classification_extraction_phase_summary.json --classifications-jsonl data/classification_extraction_phase_classifications.jsonl --entities-jsonl data/classification_extraction_phase_entities.jsonl --only-labeled
```

如果只想继续处理清洗阶段筛出来的高危语料：

```powershell
python scripts/run_classification_extraction_phase.py --db data/collection_phase_delivery.db --source cleaned --high-risk-only --min-quality-score 0.7 --summary-out data/classification_extraction_phase_high_risk_summary.json --classifications-jsonl data/classification_extraction_phase_high_risk_classifications.jsonl --entities-jsonl data/classification_extraction_phase_high_risk_entities.jsonl
```

## 4. 关键代码入口

- `src/classifier/nlp_rule_matcher.py`
- `src/enhancement/text_intelligence.py`
- `scripts/run_classification_extraction_phase.py`

## 5. 下一步建议

1. 继续细分 `众包服务=405`，优先拆 `代投服务=221`，把“私信代发 / 群发代发 / SEO排名 / 数据筛活 / 采集投放”进一步拉开。
2. 对 `拉群语义 / 订单卡单 / 卡单玩法 / 手工做单` 这批 review-only 标签做模板复盘，继续压缩边界宽、低置信样本。
3. 对少量仍落在 `诈骗引流` 的 `打粉引流 / 拉群语义` 样本再做一次模板复盘，决定是否继续上提到 `众包服务` 或继续细拆 `代运营` 标签。
