# BlackAgent

BlackAgent 是一个**面向黑灰产公开/授权情报的查询驱动调查服务**。  
它把用户自然语言调查需求串成一条可落地链路：

`用户 query -> LLM 意图解析 -> LLM 调查规划 -> 选 source -> 外部 LLM 改写 source query -> 授权采集 -> 本地线索构建/精炼 -> 可复核输出`

当前仓库已经同时具备：

- **FastAPI 服务入口**：统一暴露调查、采集、线索构建、调度、LLM 网关等接口
- **CLI 调查入口**：不启动服务也能直接发 query 跑 investigation
- **授权 source 采集**：支持公开网页/搜索页、X、Telegram 等已授权来源
- **source 级 query rewrite**：仅对带 `query_url_template` 的 source，先让外部 LLM 改写 `search_query` 再抓取
- **本地 clue pipeline**：对 raw 数据做清洗、过滤、抽取、候选线索构建和高质量线索筛选
- **本地任务队列与调度器**：支持 cron/queue 风格的分层采集和 clue build
- **memory/sql 双后端**：本地默认可用，切到 SQLite / PostgreSQL 也有清晰边界

---

## 1. 当前核心能力

### 1.1 查询驱动 investigation

主入口：

- API：`POST /api/v1/investigations/run`
- CLI：`python scripts/run_agent_cli.py`

在线 investigation 的关键特点：

- 先做 **LLM intent parse**
- 再做 **LLM investigation plan**
- 当 clue 池没有足够结果时，进入 source collection 回退路径
- 对**带 `query_url_template` 的 source**，先做 **source 级 LLM query rewrite**
- 如果 LLM 返回不可用 JSON / 缺少 `search_query`，自动回退到 source 原有 `search_query`

相关代码：

- `src/agent/investigation_orchestrator.py`
- `src/agent/query_rewriter.py`
- `src/agent/user_request_parser.py`
- `src/backend/llm_gateway.py`

### 1.2 黑灰产公开 source 批量采集

主 catalog：

- `config/intel_sources.blackgray.yaml`

统一采集脚本：

```powershell
python scripts/collect_blackgray_all.py --fresh
```

这条链路会按配置尝试：

- 公共网页/搜索页采集
- 搜索结果页命中后的 `source_url` 补抓
- X recent-search 原生采集（有凭据时）
- Telegram 授权账号回补（有凭据时）

相关文档：

- `docs/blackgray_collection.md`
- `docs/collection_phase_delivery.md`
- `docs/telegram_collection.md`
- `docs/x_collection.md`

### 1.3 本地任务队列与调度

当前仓库已提供：

- 本地 queue job 存储
- scheduler bootstrap / tick / worker run / cycle
- 分层采集：`fast / slow / clue_build`

相关入口：

- `POST /api/v1/scheduler/bootstrap`
- `POST /api/v1/scheduler/tick`
- `POST /api/v1/scheduler/workers/run`
- `POST /api/v1/scheduler/cycle`

相关代码：

- `src/scheduling/cron_queue.py`
- `src/scheduling/layered_collection.py`

### 1.4 SQL / LLM / enforcement 后端接入

当前实现不是只停留在 mock：

- SQL：支持 memory / SQLite / PostgreSQL
- LLM：支持 OpenAI-compatible 网关
- enforcement：有明确的 dry-run / approval / token / connector 边界

详细说明见：

- `docs/real_backend.md`

---

## 2. 安全与边界

这个仓库当前明确遵守以下边界：

- 只抓取**公开或已授权** source
- `network.enabled=false` 时不会真实联网采集
- HTTP 采集只做普通 GET
- 不做登录绕过、验证码绕过、代理绕过、未授权扩源
- `query rewrite` 只作用于**带 `query_url_template` 的 source**
- 对没有 `query_url_template` 的 source，保持固定 `source_url` 语义不变

如果你在看 source catalog，判断标准可以简单理解为：

- 有 `query_url_template`：可把整条 `search_query` 注入模板，动态搜索
- 没有 `query_url_template`：`source_url` 是固定入口，不参与 query 改写

---

## 3. 环境要求

- Python `>=3.11`
- Windows PowerShell / Linux shell 均可

安装基础依赖：

```powershell
pip install -e .
```

开发测试依赖：

```powershell
pip install -e .[dev]
```

可选能力：

```powershell
pip install -e .[postgres]
pip install -e .[telegram]
```

---

## 4. 配置

主配置文件：

- `config/config.yaml`

关键配置块：

- `storage`
- `tasks`
- `scheduler`
- `network`
- `llm`
- `enforcement`

项目会自动读取仓库根目录 `.env`，但不会覆盖当前 shell 里已经存在的同名环境变量。

常见环境变量：

```powershell
$env:BLACKAGENT_STORAGE_DSN='sqlite:///data/blackagent.db'
$env:BLACKAGENT_LLM_BASE_URL='https://your-provider/v1'
$env:BLACKAGENT_LLM_API_KEY='***'
$env:BLACKAGENT_LLM_MODEL='your-model'
$env:BLACKAGENT_LLM_DRY_RUN='false'
```

---

## 5. 快速开始

### 5.1 启动 API 服务

```powershell
uvicorn main:app --reload
```

健康检查：

```powershell
curl http://127.0.0.1:8000/health
```

### 5.2 直接从 CLI 发起 investigation

使用内置 demo：

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

带真实 source catalog，并显式开启网络采集：

```powershell
python scripts/run_agent_cli.py `
  --query "找接码和群控相关线索" `
  --source-config-path config/intel_sources.blackgray.yaml `
  --enable-network `
  --show summary
```

### 5.3 真实 LLM 联调

```powershell
python scripts/smoke_llm_real.py --force-real
```

连同 investigation 一起联调：

```powershell
python scripts/smoke_llm_real.py --force-real --include-investigation
```

### 5.4 一键黑灰产 raw 采集

```powershell
python scripts/collect_blackgray_all.py --fresh
```

---

## 6. 主要 API

### 调查 / 线索

- `POST /api/v1/investigations/run`
- `POST /api/v1/clues/build`
- `POST /api/v1/pipeline/advanced/run`
- `GET /api/v1/semantic/search`

### source 采集

- `POST /api/v1/sources/collect`
- `POST /api/v1/tasks/sources/collect`
- `POST /api/v1/sources/collect/batch`

### 本地任务

- `POST /api/v1/tasks/pipeline/advanced`
- `POST /api/v1/tasks/clues/build`
- `POST /api/v1/tasks/run-pending`
- `GET /api/v1/tasks/{task_id}`

### 调度器

- `POST /api/v1/scheduler/bootstrap`
- `GET /api/v1/scheduler/status`
- `POST /api/v1/scheduler/tick`
- `POST /api/v1/scheduler/workers/run`
- `POST /api/v1/scheduler/cycle`

### 后端状态 / LLM / enforcement

- `GET /api/v1/system/backend`
- `POST /api/v1/llm/chat`
- `POST /api/v1/enforcement/execute`

---

## 7. 目录结构

```text
BlackAgent/
├─ config/          # 主配置、source catalog、主题词典
├─ docs/            # 分阶段交付与运行说明
├─ prompts/         # prompt 配置
├─ scripts/         # CLI、采集、调度、smoke 脚本
├─ src/
│  ├─ agent/        # investigation orchestrator / planner / query rewrite
│  ├─ backend/      # LLM gateway / enforcement / task backend
│  ├─ cleaner/      # 文本清洗与黑话归一
│  ├─ collector/    # 授权 source 采集
│  ├─ enhancement/  # source intake / clue quality / refine
│  ├─ pipeline/     # offline clue builder
│  ├─ retrieval/    # clue retrieval
│  └─ scheduling/   # queue + layered collection
├─ storage/         # SQL backend / review repo
└─ tests/           # pytest 用例
```

---

## 8. 测试与验证

推荐最小验证：

```powershell
python -m compileall -q src tests main.py scripts\run_agent_cli.py
pytest tests/test_query_rewriter.py tests/test_backend_services.py -q
pytest tests/test_investigation_orchestrator.py -q
```

如果要验证 API 侧的 investigation 关键路径：

```powershell
pytest tests/test_api.py -k "investigation_endpoint_uses_llm_driven_plan_and_returns_high_quality_clues or investigation_endpoint_defaults_to_all_authorized_sources or investigation_endpoint_continues_when_one_source_fetch_fails" -q
```

---

## 9. 推荐阅读顺序

如果你第一次接手这个仓库，建议按下面顺序看：

1. `README.md`
2. `docs/blackgray_collection.md`
3. `docs/real_backend.md`
4. `main.py`
5. `src/agent/investigation_orchestrator.py`
6. `src/collector/http_feed_collector.py`
7. `src/scheduling/cron_queue.py`

---

## 10. 当前最关键的仓库事实

- 这是一个**查询驱动**的调查服务，不是单纯离线规则库
- 当前在线链路已经支持**外部 LLM 先改写 source query 再抓取**
- 改写边界只在 **`query_url_template` source**
- 当前可以做**多 worker / 多 source 并发**，但默认仍是**单机本地调度**
- 远程副作用动作仍有严格的 dry-run / approval / token 安全边界

