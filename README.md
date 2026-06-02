# BlackAgent

BlackAgent 是一个**本地运行的黑灰产公开/授权情报调查 Agent**。当前版本不再对外暴露 FastAPI/HTTP 接口，所有能力通过 CLI、脚本或 `src.local_runtime.LocalAgentRuntime` 在进程内调用。

核心链路（对外展示口径压缩为 5 阶段）：

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

当前重构方向遵循 `重构.md` 的低风险过渡原则：**不推倒重写**，先在现有模块外补 `application / domain / pipeline / infra / safety` 边界，再逐步把业务逻辑从 `LocalAgentRuntime` 和 `InvestigationOrchestrator` 中下沉。

## 当前能力

- **CLI 调查入口**：`python main.py` 或 `python scripts/run_agent_cli.py`。
- **本地 runtime**：`src/local_runtime.py` 统一承接 investigation、source collect、clue build、task queue、scheduler、LLM gateway、enforcement dry-run 等内部调用。
- **Application façade**：`src/application/` 提供 investigation、task、review、report 服务边界；`LocalAgentRuntime` 通过 `src/infra/container.py` 组装依赖，保持旧入口兼容。
- **Domain 契约层**：`src/domain/` 作为跨层数据契约命名空间，当前核心流转已收敛为 `PipelineItem -> PipelineItem`；`IntelRecord / CleanedRecord / RiskClassification / ClassificationResolution / ExtractedEntity / RiskClue / EntityGraphConfig` 是主数据契约，dict 只保留给 CLI、JSON 和旧测试兼容输出。
- **授权 source 采集**：支持公开网页/搜索页、X、Telegram 等已授权来源；`network.enabled=false` 时不联网。
- **source 级 query rewrite**：仅对带 `query_url_template` 的 source 生效。
- **情报处理流水线**：`IntelligencePipeline` 已接入 Clean/Dedup/Classify/Extract/Normalize/EntityGraph/CluePromotion 真实 stage；`LLMEnrich` 和 `Correlate/Score` 作为内部增强点折叠在流水线与线索生成中，不作为对外主流程节点。分类前置 `RiskPolarityScorer` 区分公告/反诈/研究/否定语境，抽取侧通过 `EntityNormalizer` 统一邀请码、TG、URL、联系方式的 normalized/hash/masked 字段。
- **分类仲裁**：LLM 不再直接覆盖规则结果，分类结构保留 `classification.rule / classification.llm / classification.final / classification.resolution`；下游只消费 `final`，同时保留 `strategy / reason / review_required` 供人工复核和审计。
- **Clue 分层**：线索先进入 `candidate_clues`，再由 `CluePromotionStage` 按跨源、观察次数、实体支撑和防御语境规则提升为 `actionable_clues`；弱线索进入 `archived_weak_clues`，避免召回优先阶段直接放大复核负担。
- **LLM 预算与路由**：`routing_profiles.yaml + ModelRouter + BudgetController + ClueRanker + LLMValueGate` 统一控制 intent/plan/query rewrite/record enrich/clue refine 的调用、token、候选条数和时延预算；简单 query 先走规则 parser，复杂 query / runtime 黑话上下文 / live source 规划才走固定 JSON schema 的 LLM parser/plan；`BudgetController` 使用 `peek/reserve/consume` lease 语义，pre-check 不污染 ledger，真实调用异常分支会记入 failed/network ledger。
- **实体图谱库**：`storage/entity_graph.py` 支持 `entity_asset / entity_observation / entity_relation` SQLite 持久化；`RuntimeContainer` 和 `InvestigationRuntime` 注入共享 `EntityGraphStore`，`ClueRetriever` 可从跨 run entity graph 生成可追溯线索，并提供 `neighborhood / entities_seen_since / cross_source_entities / related_clues` 查询。
- **规则配置化**：`RuleRegistry` 统一加载 `risk_taxonomy.yaml / entity_patterns.yaml / slang_dictionary.yaml / context_polarity.yaml / clue_generation_rules.yaml`，分类主词、二级标签、promotion marker、防御语境、实体正则和 clue promotion 门槛均可通过配置扩展；evaluation 输出 `rule_version` 用于定位规则版本影响。
- **Safety 边界**：`src/safety/` 已接入 LLM prompt wrapping、refine 输出校验和 PII masking；LLM plan 里的执行动作必须先通过 `PolicyGuard`，不通过时直接回退规则 plan。
- **本地任务队列与调度器**：支持 cron/queue 风格的分层采集和 clue build。
- **memory/sql 双后端**：默认内存模式，可切到 SQLite/PostgreSQL。

## 运行边界

- Agent 不再启动 Web 服务，也没有 `/api/v1/...` 路由。
- `main.py` 只是 CLI 包装入口，不创建 `app` 或 `create_app`。
- `pyproject.toml` 不再依赖 FastAPI/uvicorn/httpx。
- 保留内部 HTTP feed 采集器、X/TG 等第三方接口客户端、OpenAI-compatible LLM provider 配置；这些是 agent 内部运行依赖，不是对外暴露接口。


## 交付与使用文档

- `docs/使用文档.md`：面向使用者的完整运行说明，覆盖安装、demo、文本/文件输入、联网采集、routing profile、真实 LLM、阶段脚本、结果解读和验收命令。
- `docs/交付文档.md`：按 `黑灰产情报分析Agent.docx` 逐项映射分阶段目标和核心挑战，说明每一项如何实现、对应代码/配置/脚本、交付产物和当前统计结果。
- `docs/collection_phase_delivery.md`：数据采集阶段产物说明。
- `docs/cleaning_phase_delivery.md`：智能清洗阶段产物说明。
- `docs/phase2_phase3_delivery.md`：分类 / 抽取阶段产物说明。

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

## 验证

```powershell
python -m compileall -q src tests main.py scripts\run_agent_cli.py scripts\collect_public_sources.py scripts\smoke_llm_real.py
python -m pytest tests/test_local_runtime.py tests/test_run_agent_cli.py tests/test_refactor_boundaries.py -q
python scripts/evaluate_pipeline.py `
  --gold tests/evaluation/gold_classification.jsonl `
  --entities-gold tests/evaluation/gold_entities.jsonl `
  --hard-negative tests/evaluation/hard_negative.jsonl `
  --clues-gold tests/evaluation/gold_clues.jsonl `
  --min-primary-classification-f1 0.90 `
  --min-entity-f1 0.85 `
  --max-hard-negative-fpr 0.10 `
  --max-clue-overgeneration-ratio 2.0 `
  --max-review-load-per-100-records 3.0 `
  --output data/eval_report.json

python scripts/evaluate_pipeline.py `
  --gold tests/evaluation/gold_classification.jsonl `
  --entities-gold tests/evaluation/gold_entities.jsonl `
  --hard-negative tests/evaluation/hard_negative.jsonl `
  --clues-gold tests/evaluation/gold_clues.jsonl `
  --ablation `
  --output data/eval_llm_ablation.json
```

Evaluation 中 `classification_f1` 兼容旧字段，但真实质量门禁建议使用
`primary_classification_f1 / secondary_classification_f1 / hierarchical_classification_f1`。
`--ablation` 会对比 `fast/off`、`high_recall/off`、`high_recall/mock`，输出
`classification_f1_delta / entity_f1_delta / clue_*_delta / llm_calls_delta /
tokens_per_f1_gain / tokens_per_extra_valid_clue`，用于判断 LLM record enrich
是否值得启用。
