# BlackAgent 配置目录说明

本文档描述仓库当前**保留并推荐使用**的配置文件，避免历史实验 YAML 与当前主链混淆。

## 1. 当前保留的核心配置

### 运行与后端

- `config/config.yaml`
  - 应用主配置
  - 包含 API、storage、network、llm、enforcement 等运行开关
  - LLM 真实联调可通过仓库根目录 `.env` 或 shell 环境变量覆盖；最小验证脚本为 `scripts/smoke_llm_real.py`

### 标签与主题

- `config/label_schema.json`
  - 标签体系结构定义
- `config/theme_synonyms.yaml`
  - 主题同义词 / 黑话映射
  - collector/relevance 与测试都会使用

### 公共/黑灰产采集 catalog

- `config/intel_sources.public.yaml`
  - 对外公开联调样例 catalog
  - 适合本地验证 HTTP source collect/batch collect
- `config/intel_sources.blackgray.yaml`
  - 当前黑灰产公共采集主 catalog
  - 适合 `scripts/collect_blackgray_all.py`

### Telegram / X 示例配置

- `config/telegram_watch.example.yaml`
  - Telethon 采集示例
- `config/x_watch.example.yaml`
  - X recent-search 采集示例

### Telegram 公共页面采集

- `config/telegram_public_delivery_channels.yaml`
  - 当前保留的公共 Telegram 页面采集主配置
  - 适合 `scripts/collect_telegram_public_delivery.py`

## 2. 本次清理移除的历史实验配置

以下文件已从仓库移除，因为它们只服务于阶段性实验、轮次探测或变种召回试跑，当前主链不再依赖：

- `config/intel_sources.blackgray.variant_focus.yaml`
- `config/intel_sources.blackgray.variant_wave.yaml`
- `config/telegram_public_delivery_channels_media_focus.yaml`
- `config/telegram_public_delivery_channels_round3_probe.yaml`
- `config/telegram_public_delivery_channels_round3_probe2.yaml`
- `config/telegram_public_delivery_channels_v3.yaml`
- `config/telegram_public_delivery_channels_v4.yaml`
- `config/telegram_public_delivery_channels_v4b.yaml`

如果后续仍需做专项实验，建议：

1. 在 `docs/` 中先记录实验目标与保留理由；
2. 使用明确命名（例如 `*.experiment.yaml`）；
3. 实验结束后将结果回收进主配置，而不是长期并存多个轮次文件。

## 3. 推荐使用方式

### 本地公共源联调

```powershell
python scripts/collect_public_sources.py --catalog config/intel_sources.public.yaml --db data/public_collect.db --fresh
```

### 外部 LLM API 真实联调

```powershell
D:\Anaconda\python.exe scripts\smoke_llm_real.py --force-real
D:\Anaconda\python.exe scripts\smoke_llm_real.py --force-real --include-investigation
```

### Agent CLI 输入

```powershell
D:\Anaconda\python.exe scripts\run_agent_cli.py --force-real --demo-sample
D:\Anaconda\python.exe scripts\run_agent_cli.py --force-real --query "找接码和群控相关线索" --fixture-path data\my_raw_items.jsonl --show clues
```

### 黑灰产主链采集

```powershell
python scripts/collect_blackgray_all.py --public-catalog config/intel_sources.blackgray.yaml
```

### Telegram 公共页面采集

```powershell
python scripts/collect_telegram_public_delivery.py --channels-config config/telegram_public_delivery_channels.yaml
```

### Telegram / X 示例采集

```powershell
python scripts/telegram_telethon_collector.py --config config/telegram_watch.example.yaml --once
python scripts/x_recent_search_collector.py --config config/x_watch.example.yaml
```

## 4. 文档同步约定

今后若新增或删除配置文件，需要同步检查：

- `docs/production_automation.md`
- `docs/real_backend.md`
- `docs/blackgray_collection.md`
- `docs/collection_phase_delivery.md`
- 本文档 `docs/config_catalog.md`

确保“文档提到的配置文件”在仓库中真实存在。
