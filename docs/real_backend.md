# BlackAgent 真实后端接入说明

当前实现把原型扩展为可接真实服务的后端系统骨架，同时保留本地可跑的 sqlite/mock 模式，避免没有生产凭证时阻塞开发。

## 已接入的后端能力

### 1. SQL 持久化

文件：`storage/sql_backend.py`

支持：

- `sqlite:///...`：无需额外依赖，本地直接可跑。
- `postgresql://...` / `postgres://...`：安装可选依赖 `blackagent[postgres]` 后可连接 PostgreSQL。

表：

- `raw_records`
- `review_tasks`
- `audit_events`
- `entities`
- `task_runs`

配置示例：

```yaml
storage:
  backend: sql
  dsn: sqlite:///data/blackagent.db
  auto_create_schema: true
```

PostgreSQL 示例：

```powershell
$env:BLACKAGENT_STORAGE_DSN='postgresql://user:pass@localhost:5432/blackagent'
```

### 2. 后台任务层

文件：`src/backend/task_backend.py`

API：

- `POST /api/v1/tasks/pipeline/advanced`：提交二期/三期增强流水线任务。
- `POST /api/v1/tasks/run-pending`：本地模式执行待处理任务。
- `GET /api/v1/tasks/{task_id}`：查询任务状态；如果本地内存没有，会回退读 SQL `task_runs`。

状态：

- `PENDING`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`

### 3. OpenAI-compatible LLM 网关

文件：`src/backend/llm_gateway.py`

API：

- `POST /api/v1/llm/chat`

特点：

- 使用标准库 `urllib`，不新增必装依赖。
- 默认 `mock/dry_run`，不会联网。
- 配置真实 `base_url + api_key` 且关闭 dry-run 后，按 OpenAI-compatible `/chat/completions` 请求发送。
- 项目会自动读取仓库根目录 `.env`，但不会覆盖当前 shell 已设置的同名环境变量。
- MiMo 默认配置采用 `api-key` 鉴权、`max_completion_tokens` token 字段，并在 `extra_body` 中关闭 thinking，避免最小联调请求长时间等待。

环境变量：

```powershell
$env:BLACKAGENT_LLM_BASE_URL='https://api.xiaomimimo.com/v1'
$env:BLACKAGENT_LLM_API_KEY='...'
$env:BLACKAGENT_LLM_MODEL='mimo-v2.5'
$env:BLACKAGENT_LLM_DRY_RUN='false'
$env:BLACKAGENT_LLM_AUTH_HEADER='api-key'
$env:BLACKAGENT_LLM_MAX_TOKENS_PARAM='max_completion_tokens'
$env:BLACKAGENT_LLM_RESPONSE_FORMAT_SUPPORTED='false'
$env:BLACKAGENT_LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
```

最小真实链路联调：

```powershell
D:\Anaconda\python.exe scripts\smoke_llm_real.py --force-real
```

如果要连同 `investigations/run` 的真实 LLM 规划、意图解析、线索精炼一起验证：

```powershell
D:\Anaconda\python.exe scripts\smoke_llm_real.py --force-real --include-investigation
```

联调通过时应看到 `DIRECT` 和 `API` 输出中的 `ok=true`、`network_attempted=true`；全链路模式还应看到 `INVESTIGATION.llm_trace_summary` 里 `intent_parse`、`investigation_plan`、`source_query_rewrite`、`clue_refine` 等阶段 `llm_ok=true`。

如果后续要切回更强的 `mimo-v2.5-pro`，只需把 `BLACKAGENT_LLM_MODEL` 或 `config/config.yaml` 中的 `llm.model` 改成 `mimo-v2.5-pro`；真实联调时若出现超时，先用 `mimo-v2.5` 验证链路，再检查套餐、区域网络和模型侧响应耗时。

如果供应商模型不支持 OpenAI 的 `response_format={"type":"json_object"}`，设置 `response_format_supported: false` 或环境变量 `BLACKAGENT_LLM_RESPONSE_FORMAT_SUPPORTED=false`；BlackAgent 仍会在 prompt 中要求只返回 JSON，并在本地解析返回内容。现在本地解析除了直接 JSON 外，还会额外尝试 fenced JSON 和包裹文本中的首个 JSON 片段，以降低 query rewrite / plan / refine 的失败率。

### 3.1 Agent CLI 输入形式

不启动 uvicorn，也可以直接从命令行输入调查需求：

```powershell
D:\Anaconda\python.exe scripts\run_agent_cli.py --force-real --demo-sample
```

指定 query：

```powershell
D:\Anaconda\python.exe scripts\run_agent_cli.py `
  --force-real `
  --query "请复核最近24小时接码、群控脚本相关的高质量黑灰产线索，输出可复核证据链。" `
  --demo-sample `
  --show clues
```

传入自己的 JSONL 原始情报：

```powershell
D:\Anaconda\python.exe scripts\run_agent_cli.py `
  --force-real `
  --query "找接码和群控相关线索" `
  --fixture-path data\my_raw_items.jsonl `
  --output data\agent_result.json `
  --show clues
```

### 4. 系统状态检查

API：

- `GET /api/v1/system/backend`

返回存储、任务、LLM 是否启用、是否 dry-run 等状态。

### 5. 真实情报源 HTTP(S) 采集

文件：`src/collector/http_feed_collector.py`

API：

- `POST /api/v1/sources/collect`：从显式授权的 HTTP(S) feed 拉取原始情报，支持 `json/jsonl/csv/txt/auto`，可落 `raw_records` 并可选触发增强流水线。
- `POST /api/v1/tasks/sources/collect`：把同一采集动作放入本地任务队列，由 `POST /api/v1/tasks/run-pending` 执行。
- `POST /api/v1/sources/collect/batch`：一次装载多平台 source catalog，批量拉取贴吧 / 短视频 / 种草 / 众包 / 二手 / 暗网镜像 / 技术社区 / X / Telegram 等授权来源，并按批次统一落库、统一触发二三期增强流水线。

默认安全边界：

- `network.enabled=false` 时不会联网。
- 可配置 `network.allowed_domains` 限制真实 feed 域名。
- 只做普通 GET，不做登录态绕过、验证码绕过、代理绕过或未授权扩源。
- `legal_basis` 必须是授权来源类型。
- HTML 页面可通过 `feed_format=html` 直接抓页面快照文本，适合贴吧、技术社区、公开论坛帖等页面级原始采集；JSON/JSONL 源可通过 `text_fields` 指定消息正文键，适配 X / Telegram / 短视频评论导出。

配置示例：

```yaml
network:
  enabled: true
  allowed_domains: [urlhaus-api.abuse.ch]
  timeout_seconds: 15
  max_records_per_fetch: 100
```

示例请求：

```json
{
  "source_url": "https://urlhaus-api.abuse.ch/v2/files/exports/${AUTH_KEY}/recent.csv",
  "source_name": "urlhaus_recent_csv",
  "source_type": "THREAT_INTEL",
  "legal_basis": "PUBLIC_COMPLIANT_DATA",
  "feed_format": "csv",
  "persist_raw": true,
  "run_pipeline": true
}
```

批量 catalog 示例：见 `config/intel_sources.public.yaml`（公开联调）或 `config/intel_sources.blackgray.yaml`（黑灰产主链），都可直接作为 `/api/v1/sources/collect/batch` 的 `source_config_path`。

公开可达联调样例：`config/intel_sources.public.yaml` + `python scripts/collect_public_sources.py --fresh`，用于一次性验证贴吧 / 短视频 / 种草社区 / 众包 / 二手 / 暗网网关 / 技术社区 / X / Telegram 群组的 live source hub 打通与原始数据落库。

### 6. 生产处置安全网关

文件：`src/backend/enforcement.py`

API：

- `POST /api/v1/enforcement/execute`

支持候选动作：

- `ban`
- `block`
- `blacklist`
- `intercept`

真正执行前必须同时通过：

- `enforcement.enabled=true`
- `enforcement.dry_run=false`
- `confidence >= enforcement.min_confidence`
- 人工审批字段通过
- 生产安全 token 校验通过
- 配置了真实 connector，例如 `connector=webhook` + `webhook_url`

未满足条件时只会返回 `BLOCKED`、`REVIEW_REQUIRED` 或 `DRY_RUN`，并写入审计事件；不会产生生产副作用。

## 本地验证

```powershell
python -m compileall -q .
python -m pytest -q
```

当前验证结果：`42 passed`。

## 仍未自动执行的生产动作

- 不自动连接真实 PostgreSQL，除非显式配置 DSN。
- 不自动调用真实 LLM，除非显式配置 API key 且关闭 dry-run/mock。
- 不自动执行封禁、拦截、拉黑或写生产策略，除非显式打开 enforcement、关闭 dry-run、提供审批与生产安全 token，并配置真实 connector。
- 真实 Celery/Redis 可作为后续替换 `TaskBackend` 的服务实现。
