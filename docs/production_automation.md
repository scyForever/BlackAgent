# 生产化自动采集与处置接入说明

本仓库把“真实情报源自动采集、生产库持久化、封禁/拦截/拉黑处置”保留为本地 runtime 能力，但不再提供对外 HTTP API。默认安全姿态不变：采集需要显式开启网络，生产处置默认只 dry-run，并且真正执行必须经过配置开关、人工审批、置信度阈值、生产安全 token 和实际 connector。

## 1. 自动联网采集真实情报源

本地入口：

- `LocalAgentRuntime.collect_source(...)`
- `LocalAgentRuntime.collect_sources_batch(...)`
- `python scripts/collect_public_sources.py --fresh`

支持格式：`json`、`jsonl`、`csv`、`txt`、`html`、`auto`。

配置开关：

```yaml
network:
  enabled: true
  allowed_domains: [urlhaus-api.abuse.ch]
  timeout_seconds: 15
  max_records_per_fetch: 100
```

调用示例：

```python
from src.config_loader import load_settings
from src.local_runtime import LocalAgentRuntime

runtime = LocalAgentRuntime(load_settings())
try:
    result = runtime.collect_source(
        {
            "source_url": "https://urlhaus-api.abuse.ch/v2/files/exports/${AUTH_KEY}/recent.csv",
            "source_name": "urlhaus_recent_csv",
            "source_type": "THREAT_INTEL",
            "legal_basis": "PUBLIC_COMPLIANT_DATA",
            "feed_format": "csv",
        },
        persist_raw=True,
        run_pipeline=True,
    )
finally:
    runtime.close()
```

安全边界：

- URL 必须是显式 `http(s)` 地址。
- 默认不联网，必须先把 `network.enabled` 改为 `true`。
- 如果设置 `allowed_domains`，请求主机必须命中白名单。
- URL 内不允许嵌入账号密码；授权信息应通过环境变量/请求头注入。
- `legal_basis` 必须属于授权来源集合。
- 采集器只做普通 GET，不做验证码、登录态、代理绕过或反爬规避。

## 2. 自动连生产库

```powershell
$env:BLACKAGENT_STORAGE_DSN='postgresql://user:pass@prod-host:5432/blackagent'
```

配置：

```yaml
storage:
  backend: sql
  dsn: ${BLACKAGENT_STORAGE_DSN}
  auto_create_schema: true
```

`collect_source(..., persist_raw=True)` 会把原始情报写入 `raw_records`。如果 `run_pipeline=True`，还会把增强流水线产出的实体、策略候选和审计事件写入对应 SQL 表。

## 3. 自动封禁/拦截/拉黑网关

本地入口：`LocalAgentRuntime.execute_enforcement(...)`

默认配置：

```yaml
enforcement:
  enabled: false
  dry_run: true
  require_human_approval: true
  min_confidence: 0.95
  connector: audit
  require_production_token: true
```

真正执行生产动作需要同时满足：

1. `enforcement.enabled=true`
2. `enforcement.dry_run=false`
3. action 命中 `allowed_actions` 与 `allowed_target_types`
4. `confidence >= min_confidence`
5. `human_approved=true` 或本地调用传入 `approved=True`
6. 请求携带的 `production_safety_token` 与 `BLACKAGENT_PRODUCTION_SAFETY_TOKEN` 一致
7. 配置真实 connector，例如 `connector=webhook` 且 `webhook_url` 不为空

所有处置请求都会落 `audit_events`，即使结果是 `BLOCKED`、`REVIEW_REQUIRED` 或 `DRY_RUN`。
