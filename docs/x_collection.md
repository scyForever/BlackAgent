# X 原生 recent-search 采集

当前仓库除了公开搜索页链路，还新增了一条 X 原生 recent-search 采集脚本：

- 配置：`config/x_watch.example.yaml`
- 脚本：`scripts/x_recent_search_collector.py`

## 启动

```powershell
$env:BLACKAGENT_X_BEARER_TOKEN='你的 bearer token'
python scripts/x_recent_search_collector.py --config config/x_watch.example.yaml
```

## 行为

1. 逐条执行 `x.watch.queries`
2. 调用 X recent-search API
3. 只保留命中 `collection.include_keywords` 且未命中 `collection.exclude_keywords` 的推文
4. 把原始数据写入 SQLite `raw_records`
5. 用 `since_id` 做增量状态保存

## 额外字段

- `post_id`
- `author_id`
- `author_username`
- `conversation_id`
- `lang`
- `query`
- `public_metrics`
- `matched_keywords`
- `excluded_keywords`
- `keyword_hit_count`
- `relevance_version`
