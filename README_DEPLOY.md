# pet-consult 部署说明（2026-08-19 更新）

## 一、当前状态速览

| 项 | 状态 |
|---|---|
| 队列化 | PG 任务表 + Outbox + RocketMQ 发布（pyrocketmq 0.3.4）+ 8 Worker（PG 轮询消费） |
| SSE | 队列化模式为心跳+final；逐 token（Redis Stream 通道）为 Phase 2.6 待办 |
| RAG | shadow 模式 + fast 直答引用卡片；grounded 需兽医审核后灰度 |
| 生成 | LocalOpenAIAdapter（本地 9B FP8，json_schema 结构化输出）；可切 DeepSeek 过渡 |
| 多宠物 | pet_info 数组 + pet_ref + 歧义追问（详见 docs/API.md） |

## 二、Docker 部署（compose.yaml）

### 服务清单（8 个）

```text
postgresql             PostgreSQL 16（consult 库）
redis                  Redis 7
consult-vllm-vision    4B 视觉 vLLM（:8001 容器内，GPU 0）
consult-vision-gateway VisionGateway（:8102，API 的 /v1/vision 契约转发）
consult-vllm-text      9B 生成 vLLM（:8002 容器内，GPU 1；local-model profile）
rocketmq-namesrv       问诊独立 namesrv（宿主 :9877）
rocketmq-broker        问诊独立 broker（宿主 :10912，集群 pet-consult）
consult-api-1          问诊 API（宿主 :8100）
```

### 依赖与版本（requirements.lock 锁定）

```text
基础业务：fastapi 0.136.3 / pydantic 2.13.4 / sqlalchemy 2.0.52 / asyncpg 0.31.0 ...
BGE 检索：torch 2.13.0+cpu / FlagEmbedding 1.4.0 / numpy 2.3.5 / pandas 2.2.3
MQ 发布：pyrocketmq 0.3.4 / jpype1 1.7.1（镜像内置 Java 21 + RocketMQ 4.9.8 发行包）
vLLM 镜像：v0.27.1+（SM120 必需，v0.26 仅 44 t/s）
```

### 构建与启动

```bash
# 1. 配置（必填 MODEL_DIR / PG_PASSWORD）
cp .env.docker.example .env.docker && vi .env.docker

# 2. 构建 API 镜像（含 VisionGateway 复用同一镜像）
docker compose build consult-api-1

# 3. 启动（本地 9B 方案）
docker compose --profile gpu up -d

# 或 DeepSeek 过渡（不启动本地 9B）
docker compose up -d
```

### 运维速查

```bash
docker compose ps                        # 全服务状态
docker compose logs -f consult-api-1     # API 日志(含 queue_metrics/outbox)
docker compose restart consult-api-1     # 改代码后
docker compose down && docker compose up -d   # 干净重启
```

## 三、宿主机直跑（非 Docker，AutoDL 实测模式）

### 依赖安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock
pip install torch==2.13.0+cpu --index-url https://download.pytorch.org/whl/cpu
```

### 启动顺序

```bash
bash scripts/start_redis.sh                 # Redis
bash scripts/rocketmq_consult.sh start     # 问诊独立 RocketMQ(:9877/:10912)
bash scripts/start_models.sh               # 4B 视觉 + VisionGateway + 9B-FP8
bash scripts/start_api.sh                  # API(8 Worker + 队列化)
bash scripts/status_all.sh                 # 状态检查
```

### 模型栈（单卡共存 / 2 卡各独占）

```text
单卡共存：4B util 0.37(:8101) + 9B FP8 util 0.45(:8001) + VisionGateway(:8102)
2 卡部署：GPU0=4B 独占(util 0.85, max-num-seqs 8+)；GPU1=9B 独占(util 0.85, max-num-seqs 16+)
   -> 容量：单卡 ~5 并发(0.8 QPS)；2 卡预计 2-3 倍(需复测)
```

## 四、已知限制与待办

1. 消费端仍 PG 轮询（RocketMQ 仅作发布通知），PushConsumer 切换待做；
2. SSE 无逐 token（Redis Stream 通道 Phase 2.6 待接）；
3. RAG grounded 需兽医审核后灰度（当前 shadow + 直答引用卡片）；
4. 性能基线：1 并发 P50 3.7s / 5 并发 6.3s / 10 并发雪崩（详见诊断文档 §2.5-2.7）；
5. 9B JSON 合规：已用 json_schema guided decoding（成功率提升），重试 1 次兜底；
6. 服务开机自启与最小监控集待完善（无运维场景必需）。
