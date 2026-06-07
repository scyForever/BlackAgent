# Telegram 情报源持续采集

这条链路建议用 **Telethon** 实现 Telegram 用户态采集；`mirai-api-http` 不用于 Telegram，本质上是 **Mirai QQ / TIM bot framework** 的 HTTP 接口插件，适合后续扩到 QQ 侧采集时单独并行部署。

## 为什么用 Telethon

- 可以用用户态 `TelegramClient` 登录并维护会话。
- 支持历史回补：`iter_messages` / `get_messages`
- 支持实时监听：`events.NewMessage`
- 支持加入公开频道/群：`JoinChannelRequest`
- 支持邀请码进群：`ImportChatInviteRequest`

## 当前仓库落点

- 配置样例：`config/telegram_watch.example.yaml`
- 采集脚本：`scripts/telegram_telethon_collector.py`
- 落库目标：SQLite `raw_records`

## 运行前准备

1. 安装 Telethon

```powershell
pip install telethon
```

2. 配环境变量或直接改配置

```powershell
$env:BLACKAGENT_TG_API_ID='123456'
$env:BLACKAGENT_TG_API_HASH='xxxxxxxxxxxxxxxx'
$env:BLACKAGENT_TG_PHONE='+8613xxxxxxxxx'
```

3. 修改 `config/telegram_watch.example.yaml`

- `watch.keywords`：用于搜索候选公开群组/频道
- `watch.usernames`：直接指定已知公开群组/频道用户名
- `watch.invite_links`：指定已知邀请链接
- `collection.include_keywords`：真正入库前的黑灰产消息过滤词；不填时回退到 `watch.keywords`
- `collection.exclude_keywords`：防御性/新闻性排除词，避免把普通通报类消息落成原始情报
- `collection.min_keyword_hits`：至少命中多少个关键词才入库

## 启动

## 推荐首次验证流程

配置好 `BLACKAGENT_TG_API_ID`、`BLACKAGENT_TG_API_HASH`、`BLACKAGENT_TG_PHONE` 和代理后，先跑小规模一次性回补：

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once --username-limit 2 --history-limit 10 --search-limit 2
```

CLI 入口等价命令：

```powershell
python scripts/run_agent_cli.py --collect-telegram --telegram-once --telegram-username-limit 2 --telegram-history-limit 10 --telegram-search-limit 2
```

调度器入口：

```powershell
python scripts/run_collection_scheduler.py --cycles 1 --telegram-username-limit 2 --telegram-history-limit 10 --telegram-search-limit 2
```

输出 JSON 中重点看：

- `tracked_chat_count`
- `persisted_count`
- `failed_target_count`
- `targets[].status`
- `targets[].error_stage`

## 常规启动

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml
```

只做一次回补并退出：

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once
```

首次运行会触发登录；成功后会：

1. 按关键词搜索候选公开群组/频道
2. 对配置的用户名 / 邀请链接执行加入
3. 回补最近 `history_limit_per_chat` 条消息
4. 持续监听新消息
5. 只把命中黑灰产关键词且未命中排除词的消息写入 `raw_records`

## 数据结构

每条消息会带上这些扩展字段：

- `chat_id`
- `chat_title`
- `chat_username`
- `message_id`
- `sender_id`
- `sender_username`
- `reply_to_msg_id`
- `has_media`
- `matched_keywords`
- `keyword_hit_count`

## 说明

- 如果目标只是 Telegram，不需要 `mirai-api-http`
- 如果后续要把 QQ 群也并入同一情报流水线，再单独部署 Mirai，并把它作为第二条 IM source 接入
