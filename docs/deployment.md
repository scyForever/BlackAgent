# BlackAgent 部署说明

> 范围：用于答辩/内网只读演示和离线评估复跑，不提供生产公网多租户 API，不自动执行封禁、处置或外部平台写操作。

## 1. 本机只读 demo

```powershell
python scripts\serve_demo_api.py --oneshot-output data\demo_api_report.json
python scripts\serve_demo_api.py --host 127.0.0.1 --port 8765
```

- 默认使用本地 demo 样本和 `fast` profile。
- 默认不联网、不调用真实 LLM、不访问私群/验证码/登录态资源。

## 2. Docker 演示容器

```powershell
docker build -t blackagent-demo:local .
docker run --rm -p 8765:8765 blackagent-demo:local
```

打开 `http://127.0.0.1:8765` 查看本机 UI/API。

容器默认执行：

```text
python scripts/serve_demo_api.py --host 0.0.0.0 --port 8765
```

## 3. 服务器部署边界

- 建议放在内网或答辩临时服务器后面，只开放给评委/团队可访问的白名单网络。
- 若要接入真实 X / Telegram / HTTP feed，必须显式配置授权凭据、合法来源和 `network.enabled=true`。
- 外部 LLM 只应通过 OpenAI-compatible provider 配置启用，并用 `LLMValueGate` / `BudgetController` 控制 token、调用次数和时延。
- 生产化前还需要补：身份认证、审计日志集中化、持久化备份、队列守护进程、监控告警和密钥管理。

## 4. 复跑验收命令

```powershell
python scripts\build_heldout_eval.py --limit 60 --per-category 12 --output tests\evaluation\heldout_classification.jsonl
python scripts\evaluate_pipeline.py --gold tests\evaluation\heldout_classification.jsonl --classification-granularity auto --dataset-kind heldout_public_authorized_seed --profile fast --output data\eval_heldout_report.json
python scripts\run_cross_source_graph_demo.py --output data\cross_source_graph_demo_report.json
python scripts\generate_ops_dashboard.py --classification-summary data\classification_extraction_phase_high_risk_summary.json --review-records data\cleaning_phase_high_risk_corpus.jsonl --review-limit 393 --output data\ops_dashboard_report.json
```
