# 部署说明

更新日期：2026-08-14

## 规范配置

当前文档以 `compose.yaml` 为规范 Compose 文件。它使用问诊端口段 `8100-8103`。

旧 `docker-compose.yml` 仍包含 `8081/8000` 口径，未完成统一前不要与 `compose.yaml` 混用。

## Docker Compose

```bash
cd /root/autodl-tmp/projects/pet-consult
bash stack.sh init
vi .env.docker
bash stack.sh config
bash stack.sh up
bash stack.sh status
```

生产启动前必须替换所有占位密钥，并确认：

- `APP_ENV=production`
- 所有 `MOCK_*` 为 `false`
- `JWT_SIGNING_SECRET`、`LOG_HASH_SECRET`、`REDIS_PASSWORD` 已配置
- `KNOWLEDGE_API_BASE_URL`、`KNOWLEDGE_API_KEY` 和模型名已配置
- `RAG_MODE=off`
- `ENABLE_ADMIN_API=false`

## 宿主机启动

```bash
bash scripts/start_all.sh
bash scripts/status_all.sh
bash scripts/stop_all.sh
```

脚本按 Redis、可选 Guard、vLLM/VisionGateway、API 的顺序启动。Nginx 默认不自动启动。

## GPU

当前 Compose 使用 `CONSULT_GPU_DEVICE` 选择 GPU，Qwen3.5-9B 默认：

```env
CONSULT_VLLM_MAX_MODEL_LEN=8192
CONSULT_VLLM_GPU_MEMORY_UTILIZATION=0.70
TEXT_VLLM_MAX_NUM_SEQS=12
VISION_VLLM_MAX_NUM_SEQS=8
```

这些参数是进程预算，不是硬显存隔离。共享服务器必须同时监控其他容器的显存峰值。

本地 27B 生成模型尚未进入当前 Compose，见 `LOCAL_MODEL_PLAN.md`。

## 上线校验

```bash
.venv/bin/python scripts/verify_deepseek_contract.py --confirm
.venv/bin/python scripts/validate_rag_assets.py
.venv/bin/python scripts/evaluate_rag_shadow.py
.venv/bin/python -m pytest -q
curl -fsS http://127.0.0.1:18100/health/ready
```

只有实际执行成功的结果才可写入发布记录。
