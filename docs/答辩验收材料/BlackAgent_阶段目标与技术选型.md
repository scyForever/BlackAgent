# BlackAgent 阶段目标 · 核心挑战 · 技术选型详解

> 本文严格对照课题文档 `黑灰产情报分析Agent.docx` 的「分阶段目标」「核心挑战」两张表，逐项说明**怎么做的**、**用了什么技术、为什么这么选**，并给出对应的代码模块、配置和已核对指标。
> 边界：所有线索都是**人工复核候选**，不是执法定性，也不是自动处置结果；指标来自本地公开 / 授权人工 held-out，不代表线上泛化。

---

## 0. 课题原始口径（来自 Agent.docx）

**总体目标**：构建一条「情报采集 → 智能清洗 → 意图分类 → 实体抽取」的端到端自动化分析流水线，帮助业务从「人肉研判」走向「智能对抗」。
**愿景**：把散落的黑灰产情报，自动变成结构化的作弊剧本和对抗方案。
**情报三大特征**：体量大（海量原始文本/链接/账号）、噪声多（真假混杂、格式混乱、大量重复）、隐蔽强（黑话变种、谐音字、表情符号、图片文字）。

**分阶段目标（Agent.docx 表）**

| 阶段 | 目标（原文） | 产出物（原文） |
| --- | --- | --- |
| 数据采集 | 打通 IM、群组、论坛等至少 3 类情报源 | 原始情报数据集 |
| 智能清洗 | 实现自动去重、过滤噪声、识别高危内容 | 清洗后高质量语料 |
| 意图分类 | 将内容按风险类型（诈骗 / 引流 / 作弊 / 工具交易等）自动分类 | 风险分类模型 + 标签体系 |
| 实体抽取 | 抽取关键实体：风险类型、风险标签、变种黑话、引流链接、灰产账号、联系方式、标识性特征、工具资产 等 | 结构化实体库 |

**核心挑战（Agent.docx 表）**

| # | 全栈环节 | 核心挑战（原文） | 对应能力（原文） |
| --- | --- | --- | --- |
| 1 | 数据采集与处理 | 多源异构、噪声大、重复率高 | 采集框架 + 清洗 pipeline |
| 2 | 特征与数据治理 | 黑话变种、语义隐蔽 | 动态词库 + 语义归一化 |
| 3 | 模型 / Agent 选型 | 效果 / 成本 / 时延三角平衡 | LLM + NLP 协同、Agent 编排 |

---

## 1. 端到端技术架构

```mermaid
flowchart TB
  SRC["① 数据采集<br/>src/collector · config/intel_sources.*.yaml<br/>SourcePolicyGuard 合规校验 · 分层调度"] --> CLN
  CLN["② 智能清洗<br/>src/cleaner/pipeline · text_filter<br/>去重(dedup group) · 熵噪声分 · 质量/风险分"] --> CLS
  CLS["③ 意图分类<br/>src/classifier · src/pipeline/classification_resolution<br/>极性前置 · rule/llm/final 仲裁 · RuleRegistry"] --> EXT
  EXT["④ 实体抽取<br/>src/extractor · src/intelligence/entity_normalizer<br/>entity_patterns.yaml · 归一/hash/mask · 可选 BERT NER"] --> CLU
  CLU["线索生成 + 实体图谱<br/>CluePromotionStage · graph_clue_generator · EntityGraphStore<br/>candidate → actionable 分层 · 跨源/实体支撑晋级"] --> EV
  EV["证据链 + 人工复核<br/>EvidenceChain · ReviewRoute<br/>500 行可追溯 · 复核候选不自动处置"]
  ORCH["Agent 编排（贯穿全程）<br/>routing_profiles · ModelRouter · BudgetController · ClueRanker · LLMValueGate · PreflightIntent"] -. 控制预算/路由 .-> CLS
  ORCH -. .-> EXT
  ORCH -. .-> CLU
  classDef phase fill:#EFF6FF,stroke:#2563EB,color:#1E3A8A;
  classDef agg fill:#F0FDF4,stroke:#16A34A,color:#14532D;
  classDef orch fill:#F5F3FF,stroke:#7C3AED,color:#4C1D95;
  class SRC,CLN,CLS,EXT phase;
  class CLU,EV agg;
  class ORCH orch;
```

纯文本 / 打印环境（与上图等价）：

```text
                ┌──────────────── Agent 编排（贯穿全程）─────────────────┐
                │ routing_profiles·ModelRouter·BudgetController·         │
                │ ClueRanker·LLMValueGate·PreflightIntent（控制预算/路由）│
                └───────┬──────────────┬───────────────┬───────────────┘
                        ▼              ▼               ▼
①数据采集 ─▶ ②智能清洗 ─▶ ③意图分类 ─▶ ④实体抽取 ─▶ 线索生成+实体图谱 ─▶ 证据链+人工复核
collector    cleaner       classifier    extractor      CluePromotion          EvidenceChain
intel_sources text_filter  极性+仲裁      entity_norm    graph_clue·EntityGraph  ReviewRoute
SourcePolicy  去重/熵/质量  RuleRegistry  entity_patterns 跨源/实体支撑晋级       500行可追溯·不处置
```

设计原则：**规则与工程方法打底、LLM 受控增强、所有产出可追溯**。下面分阶段、分挑战说明每一处「怎么做的」和「为什么这么选」。

---

## 2. 分阶段目标：怎么做的

### 2.1 数据采集 —— 打通 IM / 群组 / 论坛等至少 3 类情报源

| 维度 | 内容 |
| --- | --- |
| 怎么做的 | `src/collector/` 按 `config/intel_sources.*.yaml` 的来源清单采集；每个 source 标注 `source_class`（IM/群组、社媒/论坛、垂直/技术、公众号/文章）；`source` 级 query rewrite 仅对带 `query_url_template` 的来源生效；分层任务队列 / 调度器做 cron/queue 风格分批采集。 |
| 技术选型 | 公开页 / 搜索页经 `r.jina.ai` + DuckDuckGo 取摘要；X、Telegram 走各自授权客户端；**默认不联网**（`network.enabled=false`）。每条 raw 保留 `source_url`、`raw_payload_uri`、`crawl/publish time`，为后续证据链留锚点。 |
| 为什么这么选 | 课题要求合规采集——不能购买数据、绕过登录、进入私群或恶意抓取。因此用「公开/授权来源 + 来源策略硬校验」而非通用爬虫。 |
| 合规闸 | `src/safety/source_policy_guard.py`：禁止绕过登录/验证码/私群，拦截 URL 中的 token/cookie/session/basic-auth。 |
| 产出与证据 | 全量 4163 raw / 83 来源（`public_compliant`）；IM/群组 3786、社媒/论坛 356、垂直/技术 21。final3 答辩主包 405 raw。 |
| 边界 | 全量 raw 偏 IM/Telegram 公开目录，**不是天然均衡**；答辩展示均衡性用四类来源各 20 行的 `external_balanced_source_evidence_pack.jsonl`。 |

### 2.2 智能清洗 —— 自动去重、过滤噪声、识别高危内容

| 维度 | 内容 |
| --- | --- |
| 怎么做的 | `src/cleaner/pipeline.py` + `src/cleaner/text_filter.py`：文本归一化（`normalize_text`）、稳定去重分组（`stable_dedup_group_id` / `canonicalize_for_dedup`）、香农熵噪声分（`shannon_entropy` / `calculate_noise_score`）、质量分（`calculate_quality_score`）、风险信号画像（`detect_risk_signal_profile`）。 |
| 技术选型 | **确定性规则 + 启发式打分**，不依赖大模型——保证海量数据下可复跑、低成本、结果稳定。 |
| 为什么这么选 | 对应「体量大、噪声多、重复率高」：清洗是高频全量环节，用确定性方法才能在固定预算内处理规模数据，且复跑结果一致便于验收。 |
| 产出与证据 | 4163 → cleaned 3464；dropped 699、重复丢弃 650；high risk 1095；平均质量分 0.7568、平均风险分 0.422。风险等级：CRITICAL 921 / HIGH 174 / MEDIUM 327 / LOW 1903 / NONE 139。 |
| 边界 | 低相关 / 白噪声样本**保留不删**——它们用于误报率与 hard-negative 评估，不能包装成风险样本。 |

### 2.3 意图分类 —— 按风险类型自动分类（产出：分类模型 + 标签体系）

| 维度 | 内容 |
| --- | --- |
| 怎么做的 | `src/classifier/nlp_rule_matcher.py` 出规则分类；分类前置 `RiskPolarityScorer`（极性判别，区分公告/反诈/研究/否定语境）；`src/pipeline/classification_resolution.py` 做**分类仲裁**：保留 `classification.rule / llm / final / resolution` 四层，LLM 不直接覆盖规则结果，下游只消费 `final`，并保留 `strategy / reason / review_required` 供人工复核与审计。 |
| 标签体系 | `config/risk_taxonomy.yaml` 定义一级风险词、promotion marker、二级标签，经 `src/rules/registry.py`（`RuleRegistry`）统一加载，可配置扩展；评估输出 `rule_version` 便于定位规则版本影响。 |
| 技术选型 | **规则主导 + 极性前置 + LLM 仲裁**，而非「全量灌大模型分类」。理由见挑战 ③：固定 token 预算下优先规则/工程方法。 |
| 产出与证据 | 全量 cleaned 分类 3464，需人工复核 970；一级分布：正常业务白噪声 1744、账号交易 513、工具交易 333、众包服务 304、诈骗引流 302、unknown 190、刷单作弊 78。人工 held-out（193 条）：一级 F1 **0.8662**、二级 0.8258、层级 0.7929、**FPR 0.0504**、分类复核率 0.1865。 |
| 边界 | 高危高质量视图一级无 `unknown`，但二级仍有「待研判/未细分」，不能说「全量未知清零」。 |

### 2.4 实体抽取 —— 抽取黑话/链接/账号/联系方式/工具等（产出：结构化实体库）

| 维度 | 内容 |
| --- | --- |
| 怎么做的 | `src/extractor/entity_extractor.py` 按 `config/entity_patterns.yaml` 正则 + 词典抽取；`src/intelligence/entity_normalizer.py`（`EntityNormalizer`）统一**邀请码、TG、URL、联系方式**的 `normalized / hash / masked` 字段；`entity_postprocessor.py` 去噪去重、`entity_risk_scorer.py` 给实体相关性打分。 |
| 技术选型 | **正则 + 词典 + 归一化打底，可选本地 BERT NER 适配**（`src/ml/local_bert.py`，默认不下载模型、不新增依赖，未配置时回退确定性规则）。 |
| 为什么这么选 | 实体抽取要求高召回 + 可结构化 + 可脱敏入库；正则/词典稳定可控，BERT 作为可插拔增强点，兼顾效果与成本。 |
| 产出与证据 | 全量实体 21774，7 类：url 12346、contact 3030、invite_code 2961、slang_term 2852、tool_name 375、settlement 110、account 100。人工 held-out 实体 **F1 0.9484**。 |
| 安全 | 落库联系方式/账号的 `canonical_hash` 可加盐（`BLACKAGENT_PII_HASH_SALT`），`src/safety/pii_masker.py` 做掩码。 |

---

## 3. 核心挑战：怎么应对的

### 3.1 挑战①：多源异构、噪声大、重复率高 → 采集框架 + 清洗 pipeline

- **多源异构**：`source_class` 把 IM/论坛/社媒/垂直/公众号统一归类，下游用同一套 `PipelineItem` 契约流转（`src/domain/`），屏蔽来源差异。
- **噪声大**：清洗阶段香农熵噪声分 + 质量分 + 防御/研究/否定语境识别，把白噪声与真实风险分开但都保留。
- **重复率高**：`stable_dedup_group_id` 稳定去重分组，重复丢弃 650 条；去重口径可复跑。
- **证据**：见 §2.1 / §2.2 数字；source smoke（IM、论坛/社媒、垂直、公众号/文章四个 `smoke_group`）证明来源覆盖。

### 3.2 挑战②：黑话变种、语义隐蔽 → 动态词库 + 语义归一化

- **动态词库**：`config/slang_dictionary.yaml` + `config/theme_synonyms.yaml` 提供黑话/谐音/主题同义词；黑话候选发现 + 灰度生命周期（`data/manual_review/slang_lifecycle_records.json`）已具备工程能力。
- **语义归一化**：`EntityNormalizer` 把同一实体的变体（邀请码、TG handle、URL、联系方式）归一为 canonical 形式，再 hash/mask；分类侧 `RiskPolarityScorer` + `config/context_polarity.yaml` 处理「语义隐蔽」中的反讽/防御/研究语境。
- **多模态隐蔽**：`src/ocr/`（`BitmapGlyphOCREngine` demo + 可选 `TesseractCliOCREngine`）把图片文字纳入 `content_modality=text/image_text/mixed`，OCR hardset 20 条覆盖 chat/poster/qr/screenshot，子串命中 20/20。
- **边界**：黑话候选与 OCR 目前是受控 hardset / probe 证明，不是大规模人工确认候选或生产 OCR 泛化。

### 3.3 挑战③：效果 / 成本 / 时延三角平衡 → LLM + NLP 协同 + Agent 编排

- **协同策略**：简单 query 走规则 parser；复杂 query / runtime 黑话上下文 / live source 规划才走固定 JSON schema 的 LLM parser/plan。`src/query/preflight_parser.py`（`PreflightIntent`）在 LLM 前先抽 risk_types/keywords/slang/freshness/cross-source 需求。
- **预算与路由**：`config/routing_profiles.yaml`（fast/balanced/high_recall）+ `config/model_stage_policy.yaml` 经 `ModelRouter` + `BudgetController`（`peek/reserve/consume` lease 语义，pre-check 不污染 ledger）+ `ClueRanker` 统一控制调用次数、token、候选条数、时延预算。
- **LLM 价值门控**：`LLMValueGate` 实测策略 `record_enrich_policy=conflict_only`、`should_enable_record_enrich=false`（`gate_reason=llm_added_cost_without_measured_quality_gain`）——**实测无收益就不开 LLM 全量增强**。
- **证据**：规模 benchmark 1 万条约 1246 条/秒、p95≈0.82ms、该路径 LLM 调用 0 次；消融报告 `eval_llm_ablation.json` / `eval_llm_hard_ablation.json` 给出 token / 时延 / F1 / 线索收益对比。
- **边界**：benchmark 只证本地吞吐与路由成本，**不证真实联网或真实 LLM 端到端时延**。

---

## 4. 技术选型决策表（为什么这么选）

| 决策 | 选了什么 | 为什么 | 取舍 / 边界 |
| --- | --- | --- | --- |
| 整体形态 | 本地 CLI / 进程内 runtime，不起公网服务 | 课题是分析 Agent 非线上风控；本地可复跑便于验收 | `serve_demo_api.py` 仅本机答辩 demo，不代表生产 API |
| Web 依赖 | 标准库 stdlib HTTP，移除 FastAPI/uvicorn/httpx | 减少依赖、降低攻击面、可离线复跑 | 不作为多租户生产服务 |
| 分类主链 | 规则主导 + 极性前置 + LLM 仲裁（rule/llm/final/resolution 四层） | 固定 token 预算、可审计、规则可配置扩展 | 二级疑难样本进人工复核，不强行定级 |
| LLM 用法 | 受控增强 + value gate（conflict_only） | 实测 record-enrich 无质量收益却增成本 | 不声称「全量 LLM 提升」 |
| 实体抽取 | 正则 + 词典 + 归一化，BERT NER 可插拔 | 高召回 + 可结构化 + 可脱敏；BERT 默认不下载 | 未配置时回退确定性规则 |
| 线索生成 | 分层晋级（candidate→actionable，弱线索归档） | 召回优先时避免直接放大人工复核负担 | 跨源/实体支撑才晋级，复核率收敛 0.1865 |
| 存储 | memory 默认 + 可切 SQLite/PostgreSQL；实体图谱独立 SQLite | 轻量可复跑 + 可持久化 + 跨 run 可追溯 | — |
| 规则配置化 | `RuleRegistry` 统一加载 6 类 YAML | 风险词/实体正则/promotion 门槛/语境词均可配 | 评估输出 `rule_version` 定位影响 |
| 安全合规 | 默认 dry-run + SourcePolicyGuard 硬规则 + PII 掩码 | 合规采集、不绕登录/私群、不自动处置 | 处置默认 `enforcement.dry_run=true` |

---

## 5. 产出闭环：风险线索 / 风险样本 / 作弊剧本（对应愿景）

- **风险线索**：`CluePromotionStage` 把候选线索按跨源、观察次数、实体支撑、防御语境规则提升为 actionable，弱线索进 archived；`graph_clue_generator.py` + `EntityGraphStore`（`src/storage/entity_graph.py`，支持 `entity_asset/observation/relation`）从跨 run 实体图谱生成可追溯线索。
- **证据链**：500 行 joined evidence pack，全部带 source URL + raw payload；其中 17 行高质量线索、8 行跨源；精选 4 条线索 / 17 条证据卡 / 缺失证据 0（`collection_phase_multi_source_clue_evidence_index.json`）。人工线索 gold 召回 F1 1.0（24 条 gold）。
- **作弊剧本与对抗方案**：`PlaybookBuilder / CountermeasureSummaryBuilder / EvidenceChainRenderer` 把线索组织成作弊剧本、复核建议和逐来源证据链——**所有对抗建议默认 review-only，不触发自动处置**（呼应 Agent.docx 愿景，同时守住边界）。

---

## 6. 边界与未尽事项

1. 当前人工确认 gold 为 **193 条**，未完成课题文档提到的「1000 条人工高精度标注离线评测集」。
2. 全量来源偏 IM/Telegram 公开目录，不是天然均衡（已用均衡证据包补充展示）。
3. OCR / 黑话候选目前是受控 hardset / probe 证明，不是生产泛化或大规模人工确认。
4. 规模 benchmark 只证本地吞吐与路由成本，不证真实联网 / 真实 LLM 端到端时延。
5. 系统输出**人工复核候选**，不自动定性、不自动封禁处置、不覆盖私群 / 登录后页面 / 验证码绕过 / 购买数据。

> 配套阅读：`BlackAgent_一图看懂.md`（速览）、`BlackAgent_真实用例速览.md`（证据链）、`BlackAgent_验收报告.md` §11–12（技术选型与架构的验收口径）。
