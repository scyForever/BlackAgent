# BlackAgent 黑灰产情报分析 Agent 阶段验收报告

**项目目录**：`D:\研一\BlackAgent`  
**报告日期**：2026-06-05  
**验收主题**：阶段目标、核心挑战、交付证据与后续优化边界

## 1. 验收结论

BlackAgent 当前已经形成本地可运行的黑灰产公开/授权情报调查 Agent：以 CLI、`src.local_runtime.LocalAgentRuntime` 和本机 stdlib demo API 为入口，围绕“情报采集 -> 智能清洗 -> 意图分类 -> 实体抽取 -> 线索生成与报告”完成端到端闭环。对照课题四个阶段目标，当前均已有对应代码、配置、脚本、数据产物和评测/验收报告支撑。

建议验收口径为：**阶段目标达成，核心挑战已给出工程化解法；同时保留 manual held-out、OCR 实体抽取、source 结构均衡等明确边界，作为下一阶段优化项。**

## 2. 课题要求摘要

课题文档要求构建一条“情报采集 -> 智能清洗 -> 意图分类 -> 实体抽取”的端到端自动化分析流水线，帮助业务从“人肉研判”走向“智能对抗”。总体目标是把散落的黑灰产情报自动变成结构化的作弊剧本和对抗方案。

启动会进一步明确：最终评判偏端到端完整度和线索质量；分类体系可以自定义，但需要合理且可被人工复核；采集必须合规，不能购买数据、恶意抓取或绕过平台限制；答辩需要讲清楚使用了哪些数据、怎么做、最终达到什么效果。

## 3. 分阶段目标验收

| 阶段 | 课题目标 | 当前实现 | 验收证据 | 结论 |
|---|---|---|---|---|
| 数据采集 | 打通 IM、群组、论坛等至少 3 类情报源，产出原始情报数据集 | source catalog、授权 HTTP collector、X/TG/公开页面采集脚本、SQLite 与 raw JSONL 导出 | `data/collection_phase_delivery_manifest.json`：raw=4163；source classes 覆盖 IM/group、social/forum、vertical/technical；`data/source_external_smoke_report.json` 三类 live smoke 均达到 ≥3 条 | 达成 |
| 智能清洗 | 自动去重、过滤噪声、识别高危内容，产出高质量语料 | CleanStage、DedupStage、quality profile、risk scorer、多模态字段物化 | `data/cleaning_phase_summary.json`：input=4163，cleaned=3464，dropped=699，duplicate_drop=650，high_risk=1095 | 达成 |
| 意图分类 | 将内容按诈骗、引流、作弊、工具交易等风险类型自动分类 | Fine-grained classifier、RiskPolarityScorer、rule/LLM/final/resolution 仲裁、可配置 taxonomy | `data/classification_extraction_phase_summary.json`：classification_count=3464；高危视图 `phase_input_count=1061` | 达成，保留待研判复核负载 |
| 实体抽取 | 抽取风险标签、黑话、链接、账号、联系方式、工具资产等，形成结构化实体库 | AdvancedEntityExtractor、EntityNormalizer、masked/hash 字段、EntityGraphStore | 全量视图 `entity_count=21774`；高危视图 `entity_count=4698`；实体图谱和线索晋升链路已接入 | 达成，OCR 实体抽取仍需增强 |

## 4. 核心挑战应对情况

### 4.1 挑战一：多源异构、噪声大、重复率高

当前系统通过 source catalog、授权 source policy、HTTP feed collector、采集元数据和清洗 pipeline 解决这一挑战。采集快照和派生阶段输入已经分开报告：raw snapshot 为 4163，cleaned/classified 输入为 3464，避免把过滤视图误说成全量 raw。

关键证据：

- source class 覆盖：IM/group 3786、social/forum 356、vertical/technical 21。
- live authorized smoke：IM/group collected=6，social/forum collected=5，vertical/technical collected=7，三类均达到 target_min_records=3。
- 清洗阶段去噪：699 条 dropped，其中 duplicate=650、generic_guide_noise=43、defensive_context_noise=6。
- quota-balanced sample 选中 1626 条，并显式暴露 social/forum、vertical/technical underfilled warning。

结论：已具备合规多源采集和清洗能力；当前主要边界是 raw corpus 仍偏向 Telegram/IM，后续应继续补齐非 IM 授权源。

### 4.2 挑战二：黑话变种、语义隐蔽、图片文字

当前系统通过 `RuleRegistry`、风险词库、上下文极性、实体归一化和多模态字段保留处理黑话与隐蔽语义。采集 manifest 已记录 variant/homophone、emoji marker 与 multimodal_text 信号。

关键证据：

- `variant_or_homophone_normalized=208`。
- `emoji_marker=186`。
- `multimodal_text=29`。
- `src/enhancement/source_intake.py` 支持 image_url、poster、caption、screenshot_ref 等多模态字段进入管线。
- `data/eval_ocr_hardset_report.json` 显示 OCR hardset 分类 F1=0.90，但实体 F1=0.4314。

结论：黑话与隐蔽表达已有规则/归一化工程链路；图片文字已进入输入合同，但生产级 OCR 实体抽取仍是当前最明确短板。

### 4.3 挑战三：效果 / 成本 / 时延三角平衡

当前系统采用规则优先、LLM 选择性介入的策略：简单 query 先走 deterministic parser 和本地规则；复杂 query、live source planning、clue refine 才按 routing profile 和 budget controller 触发 LLM。

关键证据：

- `data/eval_report.json`：classification_f1=1.0、secondary_classification_f1=0.9524、entity_f1=0.9655、llm_calls_per_1000_records=0.0、p95_latency_ms=2791.78。
- `data/eval/latest_llm_value.json`：record enrich 带来 `llm_calls_delta=145.0`，但 classification/entity/clue 指标 delta 均为 0.0，因此 `should_enable_record_enrich=false`，策略为 `conflict_only`。
- `config/routing_profiles.yaml` 支持 fast / balanced / high_recall 三档预算取舍。

结论：当前默认策略符合“效果、成本、时延平衡”的课题要求；不把大模型作为全量逐条处理默认方案。

## 5. 线索质量与可复核证据链

课题答辩关注的不是单条实体数量，而是端到端输出和线索质量。当前仓库已经把实体抽取后的结果接入实体图谱、候选线索、actionable clue 晋升、作弊剧本和证据链渲染。

关键阈值包括：

| 线索类型 | 晋升条件 | 价值 |
|---|---|---|
| 共享联系方式 | 48 小时内同 contact 至少出现 3 次 | 识别跨记录复用联系方式 |
| 共享域名 | 同 domain 来自至少 2 个 source | 提升跨源可信度 |
| 重复模板 | 去重后同模板至少 3 次 | 识别批量化推广/招募话术 |

系统输出默认 review-only：`CountermeasurePlanner` 只生成复核建议，不自动封禁、不自动处置。弱线索进入 `archived_weak_clues`，避免召回阶段放大人工复核负担。

## 6. 质量评测与边界

| 数据集 | N | 一级 F1 | 二级 F1 | 层级 F1 | 实体 F1 | 线索 F1 | 复核/100 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 主评测集 | 100 | 1.000 | 0.9524 | 1.000 | 0.9655 | 1.000 | 10.0 |
| 本地 held-out seed | 59 | 1.000 | 1.000 | 1.000 | 0.9023 | 0.000 | 61.0 |
| OCR image-text hardset | 20 | 0.900 | 0.7368 | 0.700 | 0.4314 | 0.000 | 40.0 |

需要明确的 claim boundary：

1. 主评测和 held-out seed 是本地公开/授权语料验证，不等同于线上生产效果。
2. `manual_heldout_report.json` 当前 `confirmed_record_count=0`，说明人工复核任务 CSV 已生成，但尚未形成最终人工金标。
3. OCR hardset 是当前最弱项，尤其实体 F1=0.4314，不能把图片文字能力包装成生产级 OCR。
4. dry-run、loopback 和小规模 live smoke 是 regression / compliance evidence，不等同于广泛外部平台覆盖证明。

## 7. 安全合规与运行边界

已落地控制：

- `network.enabled=false` 时默认不联网；live collection 必须显式 `--network-enabled`。
- `HTTPFeedCollector` 要求 allowed domains 和 authorized legal basis。
- `PolicyGuard` 阻断正式写入、未授权采集扩张、PII 外发和绕过限制。
- 当前交付语料 source_access_type 为 `public_compliant=4163`。
- 对抗建议默认 review-only，不触发线上处置。

运行边界：

- 当前不是完整 FastAPI / 多租户生产服务；`scripts/serve_demo_api.py` 是本机答辩 demo/API/UI。
- 不宣称覆盖私群、登录后页面、验证码页面或 robots/terms 不明确页面。
- 不把本地 smoke、loopback、seeded split 说成线上效果。

## 8. 建议验收演示路径

建议现场演示按“可运行入口 -> 阶段产物 -> 线索证据 -> 评测边界”顺序：

```powershell
python scripts/run_agent_cli.py --demo-sample --show clues
python scripts/serve_demo_api.py --oneshot-output data/demo_api_report.json
python scripts/export_delivery_corpora.py
python scripts/run_cleaning_phase.py
python scripts/run_classification_extraction_phase.py --source auto
python -m pytest -q
```

如现场时间有限，优先演示 `run_agent_cli.py --demo-sample --show clues` 和 `serve_demo_api.py --oneshot-output`，再用 JSON 报告解释四阶段产物和质量边界。

## 9. 后续优化优先级

| 优先级 | 优化项 | 目标 |
|---|---|---|
| P0 | 人工金标闭环 | 完成 59 条 held-out 人工确认，补充 clue graph gold 与冲突仲裁字段 |
| P0 | source 结构均衡 | 增加 social/forum、vertical/technical 授权源，降低 Telegram/IM 集中度 |
| P1 | OCR / 图片文字 | 注入更强 OCR engine，围绕 image-text hardset 提升实体抽取 F1 |
| P1 | 复核负载治理 | 对 `待研判` 和高 review_required 场景做规则拆分与主动学习样本回流 |
| P2 | 演示与运维面 | 把 ops_dashboard JSON 包成本机 dashboard，保留 CLI/runtime 为主入口 |

## 10. 最终结论

BlackAgent 已经可以按课题要求完成端到端黑灰产情报处理：从合规多源采集，到清洗去重、高危识别、风险分类、实体抽取，再到线索晋升、证据链和 review-only 对抗建议。当前材料足以支撑“阶段目标达成”的答辩与验收；同时报告明确保留人工金标、OCR、source skew 和线上生产化边界，避免把 smoke test 或局部验证包装成生产效果。

**建议验收结论：通过阶段验收，进入“人工金标闭环 + OCR 增强 + source 均衡 + 演示运维面”下一阶段优化。**
