# BlackAgent 本地后端接入说明

当前实现保留 SQL、任务队列、LLM 网关、授权采集、处置安全网关等后端能力，但不再通过 FastAPI/HTTP 对外暴露。所有能力统一通过 CLI 或 `src.local_runtime.LocalAgentRuntime` 在进程内调用。

## 1. SQL 持久化

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

## 2. 本地任务层

文件：`src/backend/task_backend.py`

通过 `LocalAgentRuntime` 调用：

- `submit_advanced_pipeline_task(...)`
- `submit_build_clues_task(...)`
- `run_pending_tasks(...)`
- `get_task(task_id)`

状态：`PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`。

## 3. OpenAI-compatible LLM 网关

文件：`src/backend/llm_gateway.py`

通过 `LocalAgentRuntime.llm_chat(...)` 或 investigation 主链调用。

特点：

- 使用标准库 `urllib`，不新增必装依赖。
- 默认 `mock/dry_run`，不会联网。
- 配置真实 `base_url + api_key` 且关闭 dry-run 后，按 OpenAI-compatible `/chat/completions` 请求发送。
- 项目会自动读取仓库根目录 `.env`，但不会覆盖当前 shell 已设置的同名环境变量。

真实链路联调：

```powershell
python scripts\smoke_llm_real.py --force-real
python scripts\smoke_llm_real.py --force-real --include-investigation
```

通过时应看到 `DIRECT` 与 `LOCAL_GATEWAY` 中 `ok=true`、`network_attempted=true`。

## 4. Agent CLI 输入形式

```powershell
python scripts\run_agent_cli.py --force-real --demo-sample
```

指定 query：

```powershell
python scripts\run_agent_cli.py `
  --force-real `
  --query "请复核最近24小时接码、群控脚本相关的高质量黑灰产线索，输出可复核证据链。" `
  --demo-sample `
  --show clues
```

## 5. 真实情报源 HTTP(S) 采集

文件：`src/collector/http_feed_collector.py`

本地调用：

- `LocalAgentRuntime.collect_source(...)`
- `LocalAgentRuntime.collect_sources_batch(...)`
- `scripts/collect_public_sources.py`

默认安全边界：

- `network.enabled=false` 时不会联网。
- 可配置 `network.allowed_domains` 限制真实 feed 域名。
- 只做普通 GET，不做登录态绕过、验证码绕过、代理绕过或未授权扩源。
- `legal_basis` 必须是授权来源类型。

公开可达联调样例：

```powershell
python scripts\collect_public_sources.py --fresh
```

## 6. 生产处置安全网关

文件：`src/backend/enforcement.py`

通过 `LocalAgentRuntime.execute_enforcement(...)` 调用。真正执行前必须同时通过：

- `enforcement.enabled=true`
- `enforcement.dry_run=false`
- `confidence >= enforcement.min_confidence`
- 人工审批字段通过
- 生产安全 token 校验通过
- 配置真实 connector，例如 `connector=webhook` + `webhook_url`

未满足条件时只会返回 `BLOCKED`、`REVIEW_REQUIRED` 或 `DRY_RUN`，并写入审计事件；不会产生生产副作用。

## 本地验证

```powershell
python -m compileall -q src tests main.py scripts\run_agent_cli.py scripts\collect_public_sources.py scripts\smoke_llm_real.py
python -m pytest -q
```

## 仍未自动执行的生产动作

- 不自动连接真实 PostgreSQL，除非显式配置 DSN。
- 不自动调用真实 LLM，除非显式配置 API key 且关闭 dry-run/mock。
- 不自动执行封禁、拦截、拉黑或写生产策略，除非显式打开 enforcement、关闭 dry-run、提供审批与生产安全 token，并配置真实 connector。
