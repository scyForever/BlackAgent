# BlackAgent

BlackAgent 是一个**本地运行的黑灰产公开/授权情报调查 Agent**。核心能力通过 CLI、脚本或 `src.local_runtime.LocalAgentRuntime` 在进程内调用。

核心链路：

```text
输入任务（query / 上传样本 / 指定 source / 时间范围）
  -> 安全与任务路由（PolicyGuard / SourcePolicyGuard / routing profile / 预算）
  -> 历史资产检索（clue pool / entity graph / semantic cache）
  -> 情报处理流水线（Clean / Dedup / Classify / Extract / Normalize / EntityGraph）
  -> 线索生成与报告（CluePromotion / SelectiveRefine / ReviewRoute / Report）
```

主流程分支：

```text
Request
  -> Route & Guard
  -> Asset Retrieval
  -> Need Fresh Data?
       ├─ No  -> Clue Promotion -> Report
       └─ Yes -> Collection / Input Records
                  -> Intelligence Pipeline
                  -> EntityGraph + Clue Promotion
                  -> Report
```

## 当前能力

- **CLI 调查入口**
- **本地 runtime**
- **Application façade**
- **Domain 契约层**
- **授权 source 采集**：支持公开网页/搜索页、X、Telegram 等已授权来源；`network.enabled=false` 时不联网。
- **source 级 query rewrite**
- **情报处理流水线**
- **分类仲裁**
- **Clue 分层**
- **评估分层**
- **LLM 预算与路由**
- **Query Preflight**
- **多轮会话**
- **实体图谱库**
- **作弊剧本与证据链**
- **规则配置化**
- **OCR / 本地模型适配器**
- **Safety 边界**
- **本地任务队列与调度器**
- **memory/sql 双后端**

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

`--demo-sample` 没有显式 `--query` 时会自动使用内置默认 query，适合非交互 smoke。

最终验收口径冻结在一个摘要文件中：

```powershell
python scripts/run_acceptance_gate.py
```

该命令会生成 `data/final_acceptance_summary.json`，并只把当前有效产物作为最终验收依据：`data/manual_heldout_eval_current.json`、`data/eval_manual_heldout_clue_recall_report.json`、多源 evidence pack 报告，以及显式运行的验收命令结果。`data/eval_report.json` 只作为历史/临时报告处理，不作为最终答辩口径。

demo/API/UI：

```powershell
# 只生成一次 JSON 结果
python scripts/serve_demo_api.py --oneshot-output data/demo_api_report.json

# 本机 UI/API（浏览器打开 http://127.0.0.1:8765）
python scripts/serve_demo_api.py --host 127.0.0.1 --port 8765
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

两条明确路径：

```powershell
# 离线稳定演示：使用内置样本和本地 evidence pack，不依赖当天网络和外部凭据。
python scripts/run_agent_cli.py --demo-sample --show summary --dry-run

# 授权联网演示：仅在已配置授权 source / X / Telegram 凭据时展示实时采集。
python scripts/run_agent_cli.py `
  --query "取当天诈骗引流相关线索" `
  --enable-network `
  --routing-profile high_recall `
  --source-config-path config/intel_sources.acceptance_telegramnav_live.yaml `
  --max-sources 4 `
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

`config/routing_profiles.yaml` 中的 `fast / balanced / high_recall` 会真实合并进运行预算：

- `fast`：默认不做 LLM intent parse、LLM plan、query rewrite 和 live collection，优先本地/demo/clue pool，最多 refine 少量线索。
- `balanced`：简单 query 优先规则解析；复杂 query、runtime 黑话上下文、live source 规划才启用有限 LLM intent/plan/query rewrite/refine，适合作为默认交互模式。
- `high_recall`：放宽 source、候选和 LLM 调用预算，适合召回优先的批处理。

默认 `config/config.yaml` 是安全 dry-run/mock 配置，不会因为本地存在 API key 就真实调用 LLM。真实 LLM 可复制 `config/config.real.example.yaml` 后显式传入，或使用 `--force-real`：

```powershell
python scripts/smoke_llm_real.py --force-real
python scripts/smoke_llm_real.py --force-real --include-investigation
```

一键黑灰产 raw 采集：

```powershell
python scripts/collect_blackgray_all.py --fresh
```

## 答辩与交付材料

`docs/答辩验收材料/` 是面向答辩与验收的最终材料（已补齐流程图 / 证据链可视化，mermaid + ASCII 双形式）：

- `BlackAgent_一图看懂.md`：一页速览——端到端流程图 + 核心数字 + 4 个真实用例 + 边界。
- `BlackAgent_真实用例速览.md`：群控 / 接码 / 群发云控 / 实名号 4 条线索的可视化证据链与逐条证据卡。
- `BlackAgent_验收报告.md`：完整验收口径、指标、需求对应与合规边界。
- `BlackAgent_阶段目标与技术选型.md`：对照课题文档逐项说明分阶段目标 / 核心挑战怎么做的 + 技术选型。

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
- `config/risk_taxonomy.yaml`：一级风险词、promotion marker、二级标签。
- `config/entity_patterns.yaml`：实体正则，可新增邀请码、联系方式、结算方式等模式。
- `config/clue_generation_rules.yaml`：候选线索生成和 promotion gate 门槛。
- `config/context_polarity.yaml`：防御、研究、否定语境词。

常见环境变量：

```powershell
$env:BLACKAGENT_STORAGE_DSN='sqlite:///data/blackagent.db'
$env:BLACKAGENT_LLM_BASE_URL='https://your-provider/v1'
$env:BLACKAGENT_LLM_API_KEY='***'
$env:BLACKAGENT_LLM_MODEL='your-model'
$env:BLACKAGENT_LLM_DRY_RUN='false'
# 生产/持久化部署应设置 PII 哈希盐，使落库的联系方式/账号 canonical_hash 加盐不可逆；
# 留空（默认）时哈希与历史基准产物保持一致，便于本地复跑。
$env:BLACKAGENT_PII_HASH_SALT='<deployment-secret-salt>'
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
├─ storage/         # SQL backend / review repo / persistent entity graph
└─ tests/           # pytest 用例
```
