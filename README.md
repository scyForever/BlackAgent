# BlackAgent

BlackAgent 是一个**本地运行的黑灰产公开/授权情报调查 Agent**。当前版本不再对外暴露 FastAPI/HTTP 接口，所有能力通过 CLI、脚本或 `src.local_runtime.LocalAgentRuntime` 在进程内调用。

核心链路：

`用户 query -> LLM 意图解析 -> LLM 调查规划 -> 选 source -> source query rewrite -> 授权采集 -> 本地线索构建/精炼 -> 可复核输出`

当前重构方向遵循 `重构.md` 的低风险过渡原则：**不推倒重写**，先在现有模块外补 `application / domain / pipeline / infra / safety` 边界，再逐步把业务逻辑从 `LocalAgentRuntime` 和 `InvestigationOrchestrator` 中下沉。

## 当前能力

- **CLI 调查入口**：`python main.py` 或 `python scripts/run_agent_cli.py`。
- **本地 runtime**：`src/local_runtime.py` 统一承接 investigation、source collect、clue build、task queue、scheduler、LLM gateway、enforcement dry-run 等内部调用。
- **Application façade**：`src/application/` 提供 investigation、task、review、report 服务边界；`LocalAgentRuntime` 通过 `src/infra/container.py` 组装依赖，保持旧入口兼容。
- **Domain 契约层**：`src/domain/` 作为跨层数据契约命名空间，当前兼容复用 `storage.schemas`，并新增 `RiskClue` 线索卡契约。
- **授权 source 采集**：支持公开网页/搜索页、X、Telegram 等已授权来源；`network.enabled=false` 时不联网。
- **source 级 query rewrite**：仅对带 `query_url_template` 的 source 生效。
- **本地 clue pipeline**：清洗、过滤、抽取、候选线索构建、高质量线索筛选；`src/pipeline/intelligence_pipeline.py` 提供可组合 stage 边界，旧链路继续通过 `OfflineClueBuilder` 运行。
- **LLM 预算与路由**：`ModelRouter + BudgetController + ClueRanker` 统一决定 clue 是否值得 LLM refine、最多花多少 token、预算耗尽时如何跳过。
- **Safety 边界**：`src/safety/` 提供 prompt wrapping、输出校验和 PII masking；原有 `PolicyGuard` 继续作为硬边界。
- **本地任务队列与调度器**：支持 cron/queue 风格的分层采集和 clue build。
- **memory/sql 双后端**：默认内存模式，可切到 SQLite/PostgreSQL。

## 运行边界

- Agent 不再启动 Web 服务，也没有 `/api/v1/...` 路由。
- `main.py` 只是 CLI 包装入口，不创建 `app` 或 `create_app`。
- `pyproject.toml` 不再依赖 FastAPI/uvicorn/httpx。
- 保留内部 HTTP feed 采集器、X/TG 等第三方接口客户端、OpenAI-compatible LLM provider 配置；这些是 agent 内部运行依赖，不是对外暴露接口。

## 快速开始

安装：

```powershell
pip install -e .
pip install -e .[dev]
```

运行内置 demo：

```powershell
python scripts/run_agent_cli.py --demo-sample
```

指定 query：

```powershell
python scripts/run_agent_cli.py `
  --query "请复核最近24小时接码、群控脚本相关的高质量黑灰产线索，输出可复核证据链。" `
  --demo-sample `
  --show clues
```

带授权 source catalog 并显式开启网络采集：

```powershell
python scripts/run_agent_cli.py `
  --query "找接码和群控相关线索" `
  --source-config-path config/intel_sources.blackgray.yaml `
  --enable-network `
  --show summary
```

控制效果 / 成本 / 时延取舍：

```powershell
python scripts/run_agent_cli.py `
  --query "找近24小时接码群控相关线索" `
  --source-config-path config/intel_sources.blackgray.yaml `
  --enable-network `
  --routing-profile high_recall `
  --max-sources 3 `
  --max-raw-records 200 `
  --max-candidate-clues 30 `
  --max-llm-refine-clues 5 `
  --max-elapsed-seconds 20
```

真实 LLM 最小联调：

```powershell
python scripts/smoke_llm_real.py --force-real
python scripts/smoke_llm_real.py --force-real --include-investigation
```

一键黑灰产 raw 采集：

```powershell
python scripts/collect_blackgray_all.py --fresh
```

## 配置

主配置：`config/config.yaml`

关键块：

- `storage`
- `tasks`
- `scheduler`
- `network`
- `llm`
- `enforcement`
- `investigation`
- `config/routing_profiles.yaml`：fast / balanced / high_recall 三角平衡预算参考。

常见环境变量：

```powershell
$env:BLACKAGENT_STORAGE_DSN='sqlite:///data/blackagent.db'
$env:BLACKAGENT_LLM_BASE_URL='https://your-provider/v1'
$env:BLACKAGENT_LLM_API_KEY='***'
$env:BLACKAGENT_LLM_MODEL='your-model'
$env:BLACKAGENT_LLM_DRY_RUN='false'
```

## 本地 runtime 示例

```python
from src.config_loader import load_settings
from src.local_runtime import LocalAgentRuntime

settings = load_settings()
runtime = LocalAgentRuntime(settings)
try:
    result = runtime.run_investigation(
        "找近24小时接码群控相关线索",
        fixture_items=[{"trace_id": "demo-1", "content_text": "群控脚本接码上车，联系 TG:core01"}],
    )
finally:
    runtime.close()
```

## 目录结构

```text
BlackAgent/
├─ config/          # 主配置、source catalog、主题词典
├─ docs/            # 分阶段交付与运行说明
├─ prompts/         # prompt 配置
├─ scripts/         # CLI、采集、调度、smoke 脚本
├─ src/
│  ├─ application/  # investigation/task/review/report application services
│  ├─ agent/        # investigation orchestrator / planner / query rewrite
│  ├─ backend/      # LLM gateway / enforcement / task backend
│  ├─ cleaner/      # 文本清洗与黑话归一
│  ├─ collector/    # 授权 source 采集
│  ├─ domain/       # RawIntelligence / RiskClue 等跨层契约
│  ├─ enhancement/  # source intake / clue quality / refine
│  ├─ infra/        # RuntimeContainer / telemetry
│  ├─ local_runtime.py
│  ├─ pipeline/     # offline clue builder / staged intelligence pipeline
│  ├─ retrieval/    # clue retrieval
│  ├─ safety/       # policy/prompt/output/PII guardrails
│  └─ scheduling/   # queue + layered collection
├─ storage/         # SQL backend / review repo
└─ tests/           # pytest 用例
```

## 验证

```powershell
python -m compileall -q src tests main.py scripts\run_agent_cli.py scripts\collect_public_sources.py scripts\smoke_llm_real.py
python -m pytest tests/test_local_runtime.py tests/test_run_agent_cli.py tests/test_refactor_boundaries.py -q
```
