# BlackAgent 答辩与验收材料

本目录是给评委的最终交付物。已按“少用英文名词、通俗中文讲清细节、能直接复查证据”的口径组织，并补齐了
**流程图 / 证据链可视化**（mermaid + ASCII 两种形式，渲染器和纯文本/打印环境都能看）。

## 建议阅读顺序

1. **`BlackAgent_一图看懂.md`** — 60 秒速览：一张端到端流程图 + 核心数字 + 4 个真实用例 + 边界。先看这一页。
2. **`BlackAgent_真实用例速览.md`** — 4 条真实线索的完整证据链（链路图 + 逐条证据卡），可追到 source URL 和 trace id。
3. **`BlackAgent_验收报告.md`** — 完整验收口径、指标、需求对应和合规边界（`.docx` 为同内容可提交版）。
4. **`BlackAgent_阶段目标与技术选型.md`** — 对照 Agent.docx 逐项说明「分阶段目标 / 核心挑战怎么做的 + 技术选型」（深入技术问答时翻这份）。

## 文件清单

| 文件 | 用途 |
| --- | --- |
| `BlackAgent_一图看懂.md` | 一页速览（流程图 + 核心数字 + 4 用例 + 能/不能证明边界） |
| `BlackAgent_真实用例速览.md` | 群控 / 接码 / 群发云控 / 实名号 4 条线索的可视化证据链与逐条证据卡 |
| `BlackAgent_验收报告.md` | 最终验收报告 Markdown 版（含端到端流程图） |
| `BlackAgent_验收报告.docx` | 由 `BlackAgent_验收报告.md` 渲染的可提交 Word 版 |
| `BlackAgent_阶段目标与技术选型.md` | 对照 Agent.docx 的分阶段目标 / 核心挑战逐项说明实现与技术选型（含分层架构 mermaid+ASCII 图） |
| `BlackAgent_答辩PPT.pptx` | 15 页答辩 PPT：封面 → 全流程 → 核心数字 → 4 真实用例 → 证据链完整性 → 规模分布 → 工程平衡 → 技术架构 → 技术选型取舍 → 边界 → 文件索引 |
| `BlackAgent_真实样例逐步明细.md` | 一次真实联网样例（75 条 → 高质量候选线索）的逐条追踪明细 |
| `BlackAgent_原始数据完整内容.md` | 同源复跑得到的原始完整行附录 |

## 怎么生成 / 复跑

- PPT 与 Word 由 `scripts/build_delivery_deck.py` **数据绑定**生成：数字读取自
  `data/final_acceptance_summary.json`、`data/collection_phase_multi_source_clue_evidence_index.json`、
  `data/collection_phase_multi_source_evidence_pack_report.json`、`data/cleaning_phase_summary.json`、
  `data/classification_extraction_phase_summary.json`；`.docx` 直接由 `BlackAgent_验收报告.md` 渲染，二者保持一致。
- 重生成命令（本机用 `D:\Anaconda\python.exe`）：

  ```powershell
  D:\Anaconda\python.exe scripts/build_delivery_deck.py
  ```

  该脚本只输出 `.pptx` / `.docx`，不改任何 `.md`；运行末尾会用 `zip_check` 校验文件可正常打开。
- `BlackAgent_真实样例逐步明细.md` 等真实样例材料由 `scripts/build_acceptance_trace_materials.py` 生成（基于 `acceptance_real_e2e` 样例，与本目录的 4 用例主叙事互为补充）。

## 口径与边界

- 文件路径、脚本名和必要报告名按原样保留，便于评委或队友回到仓库核对证据。
- 所有线索都是**人工复核候选**，不是执法定性，也不是自动处置结果；继续保留人工确认、图片文字抽取、来源结构均衡和线上生产化边界。
- 不声称生产实时、不自动封禁、不覆盖私群 / 登录后页面、不购买数据。
