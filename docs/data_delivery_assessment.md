# data 目录交付整理与可用性判断

整理日期：2026-06-10  
范围：仅评估 `data/` 路径下的文件；必要时参考 `docs/交付文档.md`、`需求文档.md` 和 `tests/evaluation/` 中的验收口径。

## 1. 课题数据交付口径

按需求和交付文档，数据侧至少要能支撑以下目标：

| 要求 | 可交付判断标准 |
| --- | --- |
| 合规采集 | 来自公开 / 授权来源，能说明 source、URL、采集时间、来源类别 |
| 多源覆盖 | 至少覆盖 IM/群组、论坛/社媒、垂直/技术或公众号/文章等多类来源 |
| 流水线闭环 | raw -> cleaned -> classification -> entities -> clue/evidence/report 能串起来 |
| 风险样本 | 单条数据完成意图分类，并至少抽取出 1 个有效风险实体 |
| 证据链 | 能追踪 raw snippet、clean text、classification、entities、source URL、snapshot/raw payload |
| 人工评测 | 人工确认 held-out gold、评测报告和边界说明齐全 |
| 安全边界 | 不包含登录 session、密钥、私群绕过、自动处置结果等不应交付内容 |

重要边界：`需求文档.md` 要求“1000 条人工高精度标注的离线评测数据集”。当前 `data/` 下没有这类 1000 条人工标注库；现有人工确认口径是 193 条 manual held-out gold，gold JSONL 实际在 `tests/evaluation/manual_heldout_classification.jsonl`。

## 2. data 目录总体盘点

当前 `data/` 共有 1017 个文件，约 192 MB。

| 类型 | 数量 | 判断 |
| --- | ---: | --- |
| `.json` | 270 | 多数是报告/manifest；需区分 final、历史、临时 |
| `.jsonl` | 57 | 核心语料、证据包、实体、分类结果 |
| `.db` | 16 | 可复跑底库和历史 run；只保留主库与最终验收库 |
| `.csv` | 11 | source 统计与人工复核任务包 |
| `.py` / `.pyc` | 548 | 来自安装检查目录，不属于数据交付 |
| `.pbm` / `.png` | 46 | OCR demo/hardset 图像；仅 OCR 交付需要 |
| `.traineddata` | 2 | Tesseract 依赖模型，不是课题数据 |
| `.session` | 1 | Telethon 会话文件，不应交付 |

## 3. 可直接交付的主数据包

这些文件能直接支撑最终验收或答辩数据口径，建议作为主交付清单。

| 文件/文件组 | 数据规模/状态 | 交付价值 | 注意事项 |
| --- | ---: | --- | --- |
| `data/final_acceptance_summary.json` | `completed`，`gate_failures=[]` | 最终验收摘要，汇总人工评测、证据包、source smoke、OCR、规模 benchmark | 部分证据包在 2026-06-10 又更新过，最终打包前建议重跑 `scripts/run_acceptance_gate.py` 刷新哈希 |
| `data/acceptance_direct_final3_delivery_manifest.json` | raw 405，cleaned 268，`completed` | 当前 final3 采集 manifest，来源分布、样本包、证据就绪过滤都较完整 | 比 final/final2/large/expanded 更新，优先用 final3 |
| `data/acceptance_direct_final3_raw_dataset.jsonl` | 405 行 | final3 原始数据集 | 可交付为最终验收原始采集快照 |
| `data/acceptance_direct_final3_cleaned_corpus.jsonl` | 268 行 | final3 清洗后语料 | 可与 cleaning summary 一起交付 |
| `data/acceptance_direct_final3_high_risk_corpus.jsonl` | 151 行 | final3 高风险子集 | 只能称为高风险筛选样本，不等同人工确认黑产 |
| `data/acceptance_direct_final3_raw_classifications.jsonl` | 405 行 | final3 raw 级分类结果 | 和 raw entities 一起支撑风险样本判定 |
| `data/acceptance_direct_final3_raw_entities.jsonl` | 1007 行实体 | final3 raw 级实体抽取 | 可交付为结构化实体证据 |
| `data/acceptance_direct_final3_classifications.jsonl` | 268 行 | final3 cleaned 级分类结果 | 支撑 cleaned 视图 |
| `data/acceptance_direct_final3_entities.jsonl` | 642 行实体 | final3 cleaned 级实体抽取 | 支撑 cleaned 视图 |
| `data/acceptance_direct_final3_hydrated_pages.jsonl` | 50 行 | hydrated 网页正文补充 | 可提升证据链质量 |
| `data/acceptance_direct_final3_defense_quota_sample.jsonl` | 269 行 | 严格均衡防御评估样本 | manifest warnings 为空，可用于答辩均衡视角 |
| `data/collection_phase_multi_source_acceptance_pack.jsonl` | 500 行 | 多源验收输入包 | 当前为主验收包之一 |
| `data/collection_phase_multi_source_evidence_pack.jsonl` | 500 行 | joined evidence pack：raw、clean、source、classification/entity/clue 串联 | 500 行都有 source evidence；其中分类 456 行、实体 453 行、high quality clue 17 行 |
| `data/collection_phase_multi_source_evidence_pack_report.json` | `completed` | 证据包完整性报告 | 当前文件比 final summary 中记录的旧哈希更新 |
| `data/collection_phase_multi_source_clue_evidence_index.json` | 1 个高质量 clue 索引 | 线索证据索引 | 可作为 answer-chain 展示材料 |
| `data/collection_phase_multi_source_curated_clues.jsonl` | 4 行 | 人工精选线索 | 辅助展示，不代表全量线索数 |
| `data/external_balanced_source_evidence_pack.jsonl` | 80 行 | 四类来源均衡小证据包 | 每类 20 行，适合答辩快速展示 |
| `data/external_balanced_source_evidence_pack_report.json` | `completed` | 均衡证据包报告 | `missing_required_fields=0` |
| `data/authorized_source_rerun_pack.jsonl` | 80 行 | 授权源复跑包 | 非 loopback 外部来源，snapshot 覆盖完整 |
| `data/authorized_source_rerun_pack_report.json` | `completed` | 授权源复跑报告 | 可证明非本地假数据的外部来源覆盖 |
| `data/source_smoke_report.json` | `completed` | dry-run 来源清单 smoke | 证明来源配置覆盖 |
| `data/source_live_smoke_report.json` | `completed` | loopback/授权 live smoke | 可交付为采集链路可运行证明 |
| `data/manual_heldout_report.json` | 200 输入，193 confirmed/corrected | 人工复核 gold 生成报告 | 输出 gold 不在 data，而在 `tests/evaluation/manual_heldout_classification.jsonl` |
| `data/manual_heldout_eval_current.json` | 193 行；primary F1 0.8662；entity F1 0.9484 | 人工 held-out 分类/实体评测 | 最终分类指标以此为准 |
| `data/eval_manual_heldout_clue_recall_report.json` | 193 行；clue recall/F1 1.0 | 人工线索 gold 召回评测 | 只能证明本地人工 gold，不证明线上泛化 |
| `data/eval_llm_ablation.json` | `completed` | LLM 价值/成本消融 | 可作为模型路由依据 |
| `data/eval_llm_hard_ablation.json` | `completed` | hard cases 消融 | 交付时需保留 provider/fallback 边界 |
| `data/latest_llm_value_report.json` | `record_enrich_policy=conflict_only` | 说明 LLM 只用于冲突/高价值复核 | 不应声称全量 LLM 提升 |
| `data/ocr_hardset_report.json` | 20 条 OCR hardset，`completed` | OCR 字段流转与图像文本合同证明 | 这是合成/受控 hardset，不是生产 OCR 泛化证明 |
| `data/scale_benchmark_report.json` | `completed` | 本地规模路由 benchmark | 不代表真实联网或真实 LLM 延迟 |

## 4. 可作为阶段交付或辅助证据

这些文件有交付价值，但不是最终唯一口径。建议放入 `supporting_artifacts/`，不要和主验收口径混在一起。

| 文件/文件组 | 数据规模/状态 | 用途 | 边界 |
| --- | ---: | --- | --- |
| `data/collection_phase_delivery.db` | raw 4163，cleaned 3464 | 阶段一/二主底库，可复跑 raw 和 cleaned | 来源明显偏 IM/TG，不能声称天然均衡 |
| `data/collection_phase_raw_dataset.jsonl` | 4163 行 | 全量 raw 导出 | 可交付为大规模公开/授权语料 |
| `data/collection_phase_quota_balanced_sample.jsonl` | 1626 行 | 配额样本 | manifest 有类别不足 warning，不如 defense sample 稳 |
| `data/collection_phase_defense_quota_balanced_sample.jsonl` | 209 行 | 三类来源严格样本 | 可用于答辩均衡说明 |
| `data/cleaning_phase_cleaned_corpus.jsonl` | 3464 行 | 全量清洗后语料 | 可支撑清洗阶段 |
| `data/cleaning_phase_high_risk_corpus.jsonl` | 1095 行 | 全量高风险子集 | 不等同人工确认风险 |
| `data/cleaning_phase_summary.json` | `completed` | 清洗统计：dropped 699，duplicate dropped 650 | 可交付 |
| `data/classification_extraction_phase_classifications.jsonl` | 3464 行 | 全量 cleaned 分类 | 有 unknown/待研判，不能声称全量清零 |
| `data/classification_extraction_phase_entities.jsonl` | 21774 实体 | 全量实体库导出 | 可交付为结构化实体证据 |
| `data/classification_extraction_phase_summary.json` | `completed` | 分类统计 | 全量含白噪声和人工复核项 |
| `data/classification_extraction_phase_high_risk_*.jsonl/json` | 1061 分类、4698 实体 | 高危高质量视图 | 辅助说明风险路径 |
| `data/entity_graph.db` | 68 asset，170 observation，606 relation | 实体图谱 demo 数据 | 可交付为图谱能力证明，不是最终主数据集 |
| `data/external_source_evidence_snapshots/` | 156 个 JSON | 证据包 raw payload / snapshot 引用 | 若交付 evidence pack，建议一并带上 |
| `data/acceptance_real_e2e_*.json/md` | 真实样例链路材料 | 端到端真实联网样例 | 文档已说明原文和清洗原因不完整，只作样例 |
| `data/report_telegram_large_20260608*` | raw 138，cleaned 94，classification 77 | Telegram 公开源演示 | 不作为最终验收主口径 |
| `data/telegram_all_channels_intel_summary.json` / `.db` | raw 1371 | Telegram 汇总底库 | 交付前需确认来源授权和敏感信息 |
| `data/manual_review/heldout_review_task.csv` | 200 行 | 人工复核过程材料 | 可交付，需和 `manual_heldout_report.json` 配套 |
| `data/manual_review/slang_lifecycle_records.json` | 1 条灰度黑话 | 黑话人工审核生命周期 demo | 只有 1 条，不能证明大规模候选发现 |
| `data/slang_candidate_report_probe.json` | 20 个候选 | 候选挖掘演示 | 文件名带 probe，只能作为探索样例 |
| `data/ops_dashboard_report.json` | `completed` | 运维看板/统计辅助 | 非核心数据集 |
| `data/defense_acceptance_report*.json` | `completed` | 防御验收辅助报告 | 可作为支持材料 |

## 5. 不建议交付或需要从主包剔除

这些文件不是最终数据资产，或存在敏感/过时/调试性质。建议保留在本地或归档，不放入课题交付包。

| 文件/文件组 | 判断 | 原因 |
| --- | --- | --- |
| `data/pkg_check_install/`、`data/pkg_check_install_current/` | 无用 | 安装检查展开目录，包含 602 个 `.py/.pyc` 副本，不是数据 |
| `data/pkg_check_wheels/`、`data/pkg_check_wheels_current/` | 无用 | wheel 构建产物，不是课题数据 |
| `data/tessdata/` | 不进数据包 | 48 MB Tesseract 语言模型，是运行依赖，不是数据交付物 |
| `data/telethon/blackagent_telegram.session` | 禁止交付 | 会话文件可能含账号登录态/敏感信息 |
| `data/telethon/blackagent_telegram.state.json` | 不建议交付 | 运行状态文件，和交付无关 |
| `data/tmp_docx/`、`data/tmp_ocr_probe/` | 无用 | 临时解包/探测目录 |
| `data/_tmp_*.json/html`、`data/_analysis_*.txt/json`、`data/_worker_*.json` | 无用 | 中间调试/分析文件 |
| `data/tmp_docx_extract.txt`、`data/mixins_segments.txt` | 无用 | 临时文本切片/抽取结果 |
| `data/manual_review/manual_heldout_classification.jsonl` | 无用 | 当前为 0 行；有效 gold 在 `tests/evaluation/manual_heldout_classification.jsonl` |
| `data/slang_candidate_report.json` | 不支撑交付 | 当前 input=0、candidate=0，不能证明黑话候选发现 |
| `data/source_external_live_smoke_report.json` | 不作为交付 | 状态为 `incomplete_live_evidence` |
| `data/eval_report.json` | 不作为交付 | `final_acceptance_summary.json` 已标记为 `stale_not_authoritative` |
| `data/eval_report_*.json`、`data/eval_fast_after_prs.json`、`data/eval_high_recall_after_prs.json`、`data/eval_llm_ablation_after_prs.json` | 历史评测 | 被 current/manual/final 报告替代 |
| `data/acceptance_direct_collect.db`、`*_large.db`、`*_expanded.db`、`*_final.db`、`*_final2.db` | 历史 run | 被 `acceptance_direct_collect_final3.db` 和 final3 JSONL 替代 |
| `data/acceptance_direct_raw_dataset.jsonl`、`*_large_*`、`*_expanded_*`、`*_final_*`、`*_final2_*` | 历史 run | 被 final3 版本替代，除非需要审计演进过程 |
| `data/collection_phase_delivery.before_variant_media_backup.db` | 备份 | 被 `collection_phase_delivery.db` 替代 |
| `data/collection_phase_incremental_rerun.*` | 辅助/历史 | 只反映 26 条增量复跑，不是主交付 |
| `data/direct_no_proxy_collect_trial.db/json` | 无用 | 试跑数据，raw 3 |
| `data/acceptance_pack_public_collect.db/json` | 无用 | DB raw 0，不支撑验收 |
| `data/blackagent_scheduler.db` | 无用 | 调度状态库，不是情报数据 |
| `data/blackagent_telegram.db` | 谨慎剔除 | 只有少量 raw，且可能涉及运行态/采集敏感边界 |
| `data/ocr_demo_*`、`data/ocr_tesseract_goal_*` | demo | 可本地保留，最终 OCR 口径用 `ocr_hardset_report.json` |

## 6. 建议交付包结构

建议不要直接压缩整个 `data/`。可以按以下结构复制主文件：

```text
delivery_data/
  00_summary/
    final_acceptance_summary.json
    docs/交付文档.md
  01_final3_collection/
    acceptance_direct_final3_delivery_manifest.json
    acceptance_direct_final3_raw_dataset.jsonl
    acceptance_direct_final3_cleaned_corpus.jsonl
    acceptance_direct_final3_high_risk_corpus.jsonl
    acceptance_direct_final3_raw_classifications.jsonl
    acceptance_direct_final3_raw_entities.jsonl
    acceptance_direct_final3_classifications.jsonl
    acceptance_direct_final3_entities.jsonl
    acceptance_direct_final3_hydrated_pages.jsonl
    acceptance_direct_final3_defense_quota_sample.jsonl
  02_evidence_pack/
    collection_phase_multi_source_acceptance_pack.jsonl
    collection_phase_multi_source_evidence_pack.jsonl
    collection_phase_multi_source_evidence_pack_report.json
    collection_phase_multi_source_clue_evidence_index.json
    collection_phase_multi_source_curated_clues.jsonl
    external_balanced_source_evidence_pack.jsonl
    external_balanced_source_evidence_pack_report.json
    authorized_source_rerun_pack.jsonl
    authorized_source_rerun_pack_report.json
    external_source_evidence_snapshots/
  03_manual_eval/
    manual_heldout_report.json
    manual_heldout_eval_current.json
    eval_manual_heldout_clue_recall_report.json
    manual_review/heldout_review_task.csv
    tests/evaluation/manual_heldout_classification.jsonl
    tests/evaluation/manual_heldout_clues.jsonl
  04_stage_corpora/
    collection_phase_delivery_manifest.json
    collection_phase_raw_dataset.jsonl
    cleaning_phase_cleaned_corpus.jsonl
    cleaning_phase_high_risk_corpus.jsonl
    cleaning_phase_summary.json
    classification_extraction_phase_classifications.jsonl
    classification_extraction_phase_entities.jsonl
    classification_extraction_phase_summary.json
  05_model_ocr_benchmark/
    eval_llm_ablation.json
    eval_llm_hard_ablation.json
    latest_llm_value_report.json
    ocr_hardset_report.json
    scale_benchmark_report.json
```

## 7. 当前缺口和下一步

1. 最终打包前建议重跑 `python scripts/run_acceptance_gate.py`，因为部分证据包在 `final_acceptance_summary.json` 之后更新，哈希和行数口径需要刷新。
2. 若必须严格满足“1000 条人工高精度标注离线评测集”，当前 `data/` 不满足；需要补齐人工标注数据，或在交付说明中明确当前只交付 193 条人工 held-out gold 与 4163 条公开/授权流水线语料。
3. `slang_candidate_report.json` 当前没有候选，不应再作为黑话候选发现证明；如要证明该点，应基于当前 cleaned/classification 重新生成非 probe 报告，并完成人工审核。
4. 交付包必须排除 `data/telethon/*.session`、安装检查目录、临时目录、历史 run、空文件和 stale 评测报告。
5. 白噪声/低相关样本不要删除；它们可用于误报率、清洗和 hard negative 评估，但不能包装成风险样本。
