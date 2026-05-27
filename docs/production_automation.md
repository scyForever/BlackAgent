# 生产化自动采集与处置接入说明

本仓库已经把“真实情报源自动采集、生产库持久化、封禁/拦截/拉黑处置”打成一条可运行链路，但默认保持安全姿态：采集需要显式开启网络，生产处置默认只 dry-run，并且真正执行必须经过配置开关、人工审批、置信度阈值、生产安全 token 和实际 connector。

## 1. 自动联网采集真实情报源

入口：`POST /api/v1/sources/collect`

批量入口：`POST /api/v1/sources/collect/batch`

支持格式：

- `json`
- `jsonl`
- `csv`
- `txt`
- `html`
- `auto`

配置开关：

```yaml
network:
  enabled: true
  allowed_domains: [urlhaus-api.abuse.ch]
  timeout_seconds: 15
  max_records_per_fetch: 100
```

请求示例：

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

安全边界：

- URL 必须是显式 `http(s)` 地址。
- 默认不联网，必须先把 `network.enabled` 改为 `true`。
- 如果设置 `allowed_domains`，请求主机必须命中白名单。
- URL 内不允许嵌入账号密码；授权信息应通过环境变量/请求头注入。
- `legal_basis` 必须属于授权来源集合。
- 采集器只做普通 GET，不做验证码、登录态、代理绕过或反爬规避。

多平台 catalog 能力：

- 当前保留的 catalog 见 `config/intel_sources.public.yaml`（公开联调样例）与 `config/intel_sources.blackgray.yaml`（黑灰产主链采集）。
- 其余历史实验/轮次 YAML 已清理，配置目录总览见 `docs/config_catalog.md`。
- HTML 页面源使用 `feed_format=html` 采整页原始文本快照；JSON/JSONL 源用 `text_fields` 指定正文字段。
- 批量接口会把全量原始记录统一落 `raw_records`，并可按批次触发增强流水线，用于跨源拼接风险线索。

## 2. 自动连生产库

已有 SQL 后端继续复用：

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

`/api/v1/sources/collect` 会在 `persist_raw=true` 时把原始情报写入 `raw_records`。如果 `run_pipeline=true`，还会把增强流水线产出的实体、策略候选和审计事件写入对应 SQL 表。

## 3. 自动封禁/拦截/拉黑网关

入口：`POST /api/v1/enforcement/execute`

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
5. `human_approved=true` 或 API 请求层传入 `approved=true`
6. 请求携带的 `production_safety_token` 与 `BLACKAGENT_PRODUCTION_SAFETY_TOKEN` 一致
7. 配置了真实 connector，例如 `connector=webhook` 且 `webhook_url` 不为空

请求示例：

```json
{
  "approved": true,
  "approval_id": "review-ticket-20260524-001",
  "production_safety_token": "<operator-token>",
  "actions": [
    {
      "action_type": "blacklist",
      "target_type": "domain",
      "target_value": "risk.example",
      "reason": "多源情报命中且人工复核通过",
      "confidence": 0.99,
      "evidence_trace_ids": ["trace-a", "trace-b"]
    }
  ]
}
```

所有处置请求都会落 `audit_events`，即使结果是 `BLOCKED`、`REVIEW_REQUIRED` 或 `DRY_RUN`。

## 4. 推荐上线步骤

1. 本地 `network.enabled=false` 跑完测试。
2. 在预发库配置 `storage.backend=sql` 和 PostgreSQL DSN。
3. 只开启 `network.enabled=true`，采集真实授权 feed，先不执行处置。
4. 检查 `raw_records`、`entities`、`audit_events` 数据质量。
5. 开启 `enforcement.enabled=true`，保持 `dry_run=true` 做影子评估。
6. 只有在误伤率、审批流和回滚方案都确认后，再配置 webhook connector 并关闭 dry-run。
