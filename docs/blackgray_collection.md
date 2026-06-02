# 黑灰产原始情报采集链路

当前仓库已经把“只保留黑灰产相关 raw 数据”的两条主链路打通：

## 0. 一键统一入口

```powershell
python scripts/collect_blackgray_all.py --fresh
```

行为：

- 先跑公共网页/搜索页链路
- 再对搜索结果里的 `source_url` 做一次源站页面补抓
- 如果配置里存在 X bearer token，则继续跑 X recent-search 原生采集
- 如果配置里存在 Telegram API 凭据，则继续跑 Telegram 授权账号回补
- 三条链路统一写入同一个 SQLite DB

默认输出库：

- `data/blackagent_blackgray_all.db`

## 0.5 黑话变种 / 谐音字 / 表情 / 图片文字补充

本轮补上的不是“再多抓一点词”，而是把 **采集前召回、采集中保留、采集后抽取** 三段一起补齐：

- **黑话变种 / 谐音字**：`加薇 / 加威 / 加围 / 拉裙 / 接ma / 号料`
- **表情符号**：`➕V / ✈️ / 🛩️ / 🎵 / 🐧 / 🍠`
- **图片文字情报**：`attachments / media / images / screenshots / ocr_blocks / text_blocks` 里的 OCR / alt / caption / poster 文本

代码落点：

- 变种归一化：`src/cleaner/text_filter.py`
- 主题检索词分层：`src/collector/relevance.py`
- 搜索 query 扩展：`src/collector/source_config.py`
- 图片文字拼接：`src/enhancement/source_intake.py`
- 黑话/隐藏实体抽取：`src/enhancement/text_intelligence.py`

新增链路信号：

- `query_term_stage=core|variant`：区分首轮高价值 query 和第二轮黑话/谐音补抓 query
- `multimodal_text_sources`：记录文本是从正文、图片 OCR、截图 alt、文本块等哪个入口拼出来的
- `multimodal_signal_count`：当前样本一共命中了多少个多模态文本入口

这样后面做复盘时，可以分清楚：

- 是正文直接命中
- 是黑话/谐音归一化后命中
- 还是图片文字补上后才命中

## 1. 公共网页/搜索页链路

- 配置：`config/intel_sources.blackgray.yaml`
- 启动：`python scripts/collect_blackgray_sources.py --fresh`
- 输出库：`data/blackagent_blackgray_sources.db`

这条链路覆盖：

- 贴吧
- 短视频搜索页
- 种草社区公开内容页
- 众包平台搜索页
- 二手平台搜索页
- 暗网公开搜索页
- 技术交流社区搜索页
- X 公开搜索页
- Telegram 公开搜索页

每个 source 都带：

- `include_keywords`
- `exclude_keywords`
- `include_themes`
- `exclude_themes`
- `query_url_template`
- `query_seed_terms`
- `query_themes`
- `query_term_limit`
- `min_keyword_hits`

当前规则面已从单一“接码 / 群控”扩到以下黑灰主题：

- 诈骗引流：`诈骗引流 / 引流 / 拉新 / 高佣 / 私聊进群 / 落地页`
- 刷单作弊：`刷单 / 补单 / 放单 / 点赞任务 / 关注任务 / 做任务 / 返佣 / 垫付 / 日结`
- 账号买卖：`账号交易 / 账号买卖 / 帐号买卖 / 卖号 / 收号 / 实名号 / 老号 / 白号 / 养号`
- 众包任务：`众包任务 / 众包 / 接单 / 任务单 / 外包任务`

只有命中黑灰产关键词、且未命中通报/反诈/曝光类排除词的 raw 才会写入 `raw_records`。

其中 `include_themes` / `exclude_themes` 支持把主题自动扩成同义词、近义表达和常见黑话再匹配，例如：

- `诈骗引流`：会同时覆盖 `导流 / 拉新 / 吸粉 / 私域 / 私聊进群 / 落地页 / 高佣`
- `刷单作弊`：会同时覆盖 `补单 / 放单 / 返佣 / 日结 / 垫付 / 点赞任务 / 关注任务`
- `账号交易`：会同时覆盖 `卖号 / 收号 / 出号 / 白号 / 老号 / 实名号 / 料子 / 号商`
- `众包任务`：会同时覆盖 `众包 / 接单 / 接任务 / 外包任务 / 代发 / 代聊 / 代做`
- `Telegram`：会同时覆盖 `TG / 飞机 / 电报 / 纸飞机`

同时，公开搜索 source 现在支持 **query 自动展开**：

- `query_seed_terms`：固定搜索骨架，如 `site:x.com telegram`
- `query_themes`：要展开的主题，如 `账号交易 / 接码 / 刷单作弊`
- `query_term_limit`：每个主题最多取多少个搜索同义词
- `query_url_template`：查询 URL 模板，内部用 `{query}` 注入并自动 URL encode

也就是说，一个 source 不必只写死一个 query；系统会基于主题词典自动改写出多组近义搜索词，再批量抓搜索结果。

## investigation 在线链路：外部 LLM 先改写 query，再抓取

当前在线 investigation 回退抓取链路新增了一层 **外部 LLM query rewrite**：

- 位置：`src/agent/query_rewriter.py`
- 接入点：`src/agent/investigation_orchestrator.py`
- 触发时机：clue 池未命中，并进入 source collection 回退路径时

执行顺序现在是：

`用户 query -> 简单 query 规则 intent / 复杂 query 固定 schema LLM intent -> 规则 plan / LLM plan -> PolicyGuard 审核动作 -> 选 source -> LLM 改写 search_query -> 按改写后的 URL 抓取`

边界保持保守：

- 只对带 `query_url_template` 的 source 做改写
- LLM 返回非 JSON、缺少 `search_query`、或者供应商兼容性不好时，会自动回退到 source 现有 `search_query`
- intent/plan 的 LLM 输出必须符合固定 schema；schema 不可用、policy guard 不通过或 LLM 调用失败时，回退规则 parser / deterministic plan
- 所以 catalog 里的静态 query 仍是保底链路，但在线 investigation 会优先尝试让外部 LLM 把 query 改得更贴近当前用户任务

现在 query 展开分成两层：

1. **core 首轮词**：高精度主词，例如 `私域导流 / 加v / 拉群 / 高佣`
2. **variant 二轮词**：黑话、谐音、表情映射词，例如 `加薇 / ➕v / 拉裙`

推荐采集策略是：

- **先跑 core**：用最小 query 数打到最高命中率
- **只对高价值主题补跑 variant**：例如 `诈骗引流 / 众包任务 / 工具交易`
- **最后再做 source_url 补抓 + 图片文字拼接**：避免一开始就把每条图片都当重处理样本

也就是：

`主题词 core 检索 -> variant 补抓 -> 结果页拆条 -> 源站补抓 -> 图片文字 OCR/alt/caption 拼接 -> Phase II/III 分类抽取`

这是当前仓库里性价比最高的一条链路：先用便宜 query 放大召回，再把昂贵的源站/图片处理放到后面。

对于 DuckDuckGo 搜索结果页，这条链路不再只存整页快照，而是会把结果页拆成**逐结果 raw**：

- `source_url`：具体命中的结果链接
- `search_query_url`：原始搜索页 URL
- `result_title`
- `result_rank`

统一入口默认还会继续把这些 `source_url` 做一次**源站页面补抓**，因此最终库里会同时保留：

- 搜索结果摘要 raw
- 源站页面快照 raw

额外保留的原始证据字段：

- `matched_keywords`
- `excluded_keywords`
- `keyword_hit_count`
- `relevance_version`

说明：

- 小红书公开搜索结果存在登录门，当前“种草社区”入口默认切到 **SMZDM（什么值得买）** 这类可公开访问的种草社区页面，保证能持续拿到黑灰相关 raw，而不是只落登录页或空搜索页。
- 主题同义词/黑话词典已外置到 `config/theme_synonyms.yaml`，后续新增表达优先改这个文件，而不是继续把词硬编码回 collector。

## 2. Telegram 授权账号链路

- 配置：`config/telegram_watch.example.yaml`
- 启动：`python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml`
- 一次性回补并退出：`python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once`

这条链路会：

1. 用 `watch.keywords` 搜公开候选群/频道
2. 加入配置里的用户名 / 邀请链接目标
3. 回补历史消息
4. 持续监听新消息
5. 仅在消息命中 `collection.include_keywords` 且未命中 `collection.exclude_keywords` 时入库

## 3. X 原生 recent-search 链路

- 配置：`config/x_watch.example.yaml`
- 启动：`python scripts/x_recent_search_collector.py --config config/x_watch.example.yaml`

这条链路会：

1. 逐条执行 `watch.queries`
2. 用 recent-search API 拉取增量结果
3. 仅在推文命中黑灰产关键词时入库
4. 保存 `since_id` 增量状态

## 当前 raw 数据形态

统一落到 SQLite `raw_records`，基础字段包括：

- `source_type`
- `source_name`
- `source_url`
- `legal_basis`
- `crawl_time`
- `publish_time`
- `content_text`

命中黑灰产过滤后，还会追加：

- `matched_keywords`
- `excluded_keywords`
- `keyword_hit_count`
- `relevance_version`
