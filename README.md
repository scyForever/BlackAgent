# BlackAgent

BlackAgent 是一个**本地运行的黑灰产公开/授权情报调查 Agent**。核心能力通过 CLI、脚本或 `src.local_runtime.LocalAgentRuntime` 在进程内调用；答辩展示可选 `scripts/serve_demo_api.py` 提供本机 stdlib HTTP demo/API/UI，不引入 FastAPI/uvicorn，也不作为生产公网服务。

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
- **评估分层**：`scripts/evaluate_pipeline.py` 同时输出一级/二级/层级分类 F1、`confusion_analysis`、`typical_errors`、`classification_review_load`、`standard_clue_eval / graph_clue_eval / overall_review_load_eval`；`scripts/build_heldout_eval.py` 可从本地公开/授权语料生成独立 held-out split，标准线索、图谱线索和人工复核负载不再混用同一口径。
- **LLM 预算与路由**：`routing_profiles.yaml + ModelRouter + BudgetController + ClueRanker + LLMValueGate` 统一控制 intent/plan/query rewrite/record enrich/clue refine 的调用、token、候选条数和时延预算；简单 query 先走规则 parser，复杂 query / runtime 黑话上下文 / live source 规划才走固定 JSON schema 的 LLM parser/plan；`BudgetController` 使用 `peek/reserve/consume` lease 语义，pre-check 不污染 ledger，真实调用异常分支会记入 failed/network ledger。
- **Query Preflight**：`src/query/preflight_parser.py` / `blackagent.query` 提供正式 `PreflightIntent`，在 LLM 解析前先抽取 risk_types、keywords、slang_terms、entity_types、freshness、preferred_source_types 和 cross-source 需求。
- **多轮会话合同**：`src/conversation/` / `blackagent.conversation` 支持“展开第 N 条线索、解释依据、追踪实体、修改 source/profile 后重跑、基于当前线索生成报告”的 follow-up 解析与 session memory。
- **实体图谱库**：`storage/entity_graph.py` 支持 `entity_asset / entity_observation / entity_relation` SQLite 持久化；`RuntimeContainer` 和 `InvestigationRuntime` 注入共享 `EntityGraphStore`，`ClueRetriever` 可从跨 run entity graph 生成可追溯线索，并提供 `neighborhood / entities_seen_since / cross_source_entities / related_clues` 查询。
- **作弊剧本与证据链**：`PlaybookBuilder / CountermeasureSummaryBuilder / EvidenceChainRenderer` 可把线索组织成作弊剧本、复核建议和逐来源证据链；所有对抗建议默认 review-only，不触发自动处置。
- **规则配置化**：`RuleRegistry` 统一加载 `risk_taxonomy.yaml / entity_patterns.yaml / slang_dictionary.yaml / context_polarity.yaml / clue_generation_rules.yaml`，分类主词、二级标签、promotion marker、防御语境、实体正则和 clue promotion 门槛均可通过配置扩展；evaluation 输出 `rule_version` 用于定位规则版本影响。
- **OCR / 本地模型适配器**：`src/ocr` 接受上游 OCR 字段或注入 OCR engine，并提供 `BitmapGlyphOCREngine` demo 与可选 `TesseractCliOCREngine`；`content_modality=text/image_text/mixed` 会进入记录元数据；`src/ml` 提供本地 BERT 分类/NER 前置适配合同，默认不下载模型、不新增依赖，未配置时回退到本地确定性规则。
- **P0-P2 展示与监控**：`scripts/run_cross_source_graph_demo.py` 展示同一 TG/域名跨 IM/论坛/授权 feed 出现时如何提升线索可信度；`scripts/generate_ops_dashboard.py` 汇总采集失败率、源覆盖、复核负载、时延、token 成本和 LLM 价值门控，输出 dashboard-friendly JSON。
- **Safety 边界**：`src/safety/` 已接入 LLM prompt wrapping、refine 输出校验和 PII masking；LLM plan 里的执行动作必须先通过 `PolicyGuard`，不通过时直接回退规则 plan。
- **本地任务队列与调度器**：支持 cron/queue 风格的分层采集和 clue build。
- **memory/sql 双后端**：默认内存模式，可切到 SQLite/PostgreSQL。

## 运行边界

- Agent 默认不启动 Web 服务，也没有生产 `/api/v1/...` 路由。
- `scripts/serve_demo_api.py` 是本机答辩 demo/UI，默认绑定 `127.0.0.1`，只调用本地样本和 runtime，不代表线上多租户 API。
- `main.py` 只是 CLI 包装入口，不创建 `app` 或 `create_app`。
- `pyproject.toml` 不再依赖 FastAPI/uvicorn/httpx。
- 保留内部 HTTP feed 采集器、X/TG 等第三方接口客户端、OpenAI-compatible LLM provider 配置；这些是 agent 内部运行依赖，不是对外暴露接口。


## 交付与使用文档

- `docs/使用文档.md`：面向使用者的完整运行说明，覆盖安装、demo、文本/文件输入、联网采集、routing profile、真实 LLM、阶段脚本、结果解读和验收命令。
- `docs/deployment.md`：本机 demo、Docker 演示容器、服务器部署边界与 P0-P2 复跑命令。
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

一键答辩 demo/API/UI：

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
python -m compileall -q src tests main.py scripts\run_agent_cli.py scripts\collect_public_sources.py scripts\smoke_llm_real.py scripts\build_heldout_eval.py scripts\export_manual_heldout_review.py scripts\validate_manual_heldout.py scripts\generate_source_smoke_report.py scripts\run_live_source_smoke.py scripts\run_ocr_demo.py scripts\build_ocr_hardset.py scripts\run_cross_source_graph_demo.py scripts\generate_ops_dashboard.py scripts\serve_demo_api.py scripts\run_scale_benchmark.py
python -m pytest -m "not integration and not network"
python -m pytest -m integration
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

python scripts/evaluate_pipeline.py `
  --gold tests/evaluation/llm_required_cases.jsonl `
  --hard-negative tests/evaluation/context_conflict.jsonl `
  --ablation `
  --with-budget `
  --write-latest-llm-value data/latest_llm_value_report.json `
  --output data/eval_llm_hard_ablation.json

python scripts/build_heldout_eval.py `
  --limit 60 `
  --per-category 12 `
  --output tests/evaluation/heldout_classification.jsonl

python scripts/export_manual_heldout_review.py `
  --input tests/evaluation/heldout_classification.jsonl `
  --output data/manual_review/heldout_review_task.csv `
  --readme data/manual_review/README.md `
  --report data/manual_review/heldout_review_task_report.json `
  --limit 60 `
  --min-target 50

# 未经人工填写 CSV 时应保持 insufficient_confirmed_records，防止 seeded label 冒充人工 gold。
python scripts/validate_manual_heldout.py `
  --input tests/evaluation/heldout_classification.jsonl `
  --review-csv data/manual_review/heldout_review_task.csv `
  --output tests/evaluation/manual_heldout_classification.jsonl `
  --report data/manual_heldout_report.json `
  --min-records 50

python scripts/evaluate_pipeline.py `
  --gold tests/evaluation/heldout_classification.jsonl `
  --classification-granularity auto `
  --dataset-name blackagent_local_public_authorized_heldout_v1 `
  --dataset-kind heldout_public_authorized_seed `
  --profile fast `
  --min-primary-classification-f1 0.90 `
  --min-secondary-classification-f1 0.80 `
  --min-hierarchical-classification-f1 0.80 `
  --max-classification-review-rate 0.70 `
  --output data/eval_heldout_report.json

python scripts/generate_source_smoke_report.py `
  --source-config config/intel_sources.public.yaml `
  --output data/source_smoke_report.json

python scripts/generate_source_smoke_report.py `
  --source-config config/intel_sources.public.yaml `
  --network-enabled `
  --max-records 3 `
  --timeout-seconds 10 `
  --output data/source_external_smoke_report.json

python scripts/run_live_source_smoke.py `
  --output data/source_live_smoke_report.json

python scripts/build_acceptance_evidence_pack.py `
  --acceptance-pack data/collection_phase_multi_source_acceptance_pack.jsonl `
  --cleaned data/acceptance_direct_final3_cleaned_corpus.jsonl `
  --classifications data/acceptance_direct_final3_raw_classifications.jsonl `
  --entities data/acceptance_direct_final3_raw_entities.jsonl `
  --hydrated data/acceptance_direct_final3_hydrated_pages.jsonl `
  --output data/collection_phase_multi_source_evidence_pack.jsonl `
  --report-out data/collection_phase_multi_source_evidence_pack_report.json

python scripts/run_ocr_demo.py `
  --output data/ocr_demo_report.json

python scripts/build_ocr_hardset.py `
  --output tests/evaluation/ocr_image_text_hardset.jsonl `
  --image-dir data/ocr_hardset_images `
  --report data/ocr_hardset_report.json `
  --count 20

python scripts/evaluate_pipeline.py `
  --gold tests/evaluation/ocr_image_text_hardset.jsonl `
  --classification-granularity auto `
  --dataset-name blackagent_ocr_image_text_hardset_v1 `
  --dataset-kind ocr_image_text_hardset `
  --profile fast `
  --output data/eval_ocr_hardset_report.json

python scripts/run_cross_source_graph_demo.py `
  --output data/cross_source_graph_demo_report.json

python scripts/generate_ops_dashboard.py `
  --classification-summary data/classification_extraction_phase_high_risk_summary.json `
  --source-smoke data/source_smoke_report.json `
  --review-records data/cleaning_phase_high_risk_corpus.jsonl `
  --review-limit 393 `
  --output data/ops_dashboard_report.json

python scripts/run_scale_benchmark.py `
  --sample-sizes 10000 100000 `
  --batch-size 2000 `
  --profile fast `
  --output data/scale_benchmark_report.json
```

Evaluation 中 `classification_f1` 兼容旧字段，但真实质量门禁建议使用
`primary_classification_f1 / secondary_classification_f1 / hierarchical_classification_f1`。
当前 `tests/evaluation/gold_classification.jsonl` 已补二级 gold，`classification_granularity=auto`
会自动进入层级评估；二级标签在没有 gold 标注时才仅作为辅助字段。
held-out 报告会额外输出 `dataset.is_heldout`、`annotation_sources`、`typical_errors`
和 `classification_review_load`；`heldout_public_authorized_seed` 只证明本地公开/授权
split，不能冒充线上泛化。人工版 held-out 只有在 `validate_manual_heldout.py`
看到 `confirmed / corrected` 且 `annotator / review_date / final_risk_categories /
conflict_handling` 等字段完整时才会输出 `manual_heldout_public_authorized`。
线索质量需分别查看 `standard_clue_eval` 与 `graph_clue_eval`，人工负载看
`overall_review_load_eval`；source smoke 使用 IM、论坛/社媒、垂直平台、
公众号/文章四个 `smoke_group`，其中公众号/文章仍保留在全局
`social_or_forum` source class 中。
`--ablation` 会保留 `fast/off`、`high_recall/off`、`high_recall/mock`，
并额外输出 `fast/off`、`balanced/mock`、`high_recall/real_or_configured_fallback`
的 LLM value matrix；真实网关未配置时高召回场景会标注 `fallback` 而不是静默省略。
小型 hard ablation 固定使用 `tests/evaluation/llm_required_cases.jsonl` 的 50 条
LLM-required 正样本和 `tests/evaluation/context_conflict.jsonl` 的 50 条冲突负样本；
credentialless/offline 环境只能声称 value gate 离线证明，不能冒充真实 provider 证明。
真实 LLM 价值证明需要配置真实网关凭据并追加 `--ablation-include-real`，否则
`high_recall/real_or_configured_fallback` 必须显示 `provider_status=fallback` 和
`fallback_reason`。
报告包含 `classification_f1_delta / entity_f1_delta / clue_*_delta / llm_calls_delta /
tokens_per_f1_gain / tokens_per_extra_valid_clue / latency_ms_per_f1_gain /
latency_ms_per_extra_valid_clue`，用于判断 LLM record enrich 是否值得启用；
默认仍是受控增强，不把 record enrich 当作全量必选链路。

默认 `pytest` 已配置为 offline/unit：`pytest.ini` 和 `pyproject.toml` 会排除
`integration`、`network` marker；需要真实网络或集成验证时显式运行对应 marker。
