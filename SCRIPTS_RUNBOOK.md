# 宠物问诊顾问项目 · 脚本运行逻辑框架文档

> 生成时间：2026-09-01　|　版本：pet-consult v7.3
> 说明：本文档只描述项目内**每个脚本/入口**的运行逻辑框架（功能、参数、执行流程、产出与依赖），**不修改任何源代码**。本文仅覆盖 `scripts/` 脚本与三个独立服务入口（`app/main.py`、`app/guard_server.py`、`app/vision_server.py`）。

---

## 0. 项目整体运行框架

### 0.1 系统定位
宠物问诊算力侧 API。负责症状信息整理、图片事实提取、风险分级、护理观察建议和就医建议，不替代执业兽医诊断。

### 0.2 服务与组件（端口）
| 组件 | 职责 | 端口 |
|---|---|---|
| `consult-api-1`（`app/main.py`） | HTTP/SSE、任务登记、Worker、问诊编排 | `127.0.0.1:18100` |
| `consult-vllm-text` | Qwen3.5-9B 文本生成 | 容器网络 `:8002` |
| `consult-vllm-vision` | Qwen3.5-4B 图片理解 | 容器网络 `:8001` |
| `consult-vision-gateway`（`app/vision_server.py`） | `/v1/vision` 到 vLLM 转换、并发队列与缓存 | `:8102` |
| `consult-guard`（`app/guard_server.py`） | Qwen3Guard 内容审核 | `:8103` |
| `postgresql` | 任务、Outbox、事件、结果、对话存档 | 容器网络 |
| `redis` | 会话、缓存、幂等、并发闸门、SSE 进度 | 容器网络 |
| `rocketmq-namesrv` / `broker` | 问诊任务消息队列 | `:9877` / `:10912` |

### 0.3 请求处理主链路
```
业务客户端 -> 业务网关(:19000) -> Consult API(:18100)
  -> 鉴权/限流/幂等/校验
  -> 文字急症规则预判（最前置，纯规则）
  -> PG：consult_task + consult_outbox 同事务登记 -> RocketMQ 发布
  -> API 容器内 Worker 并发消费
       -> 图片 -> VisionGateway -> Qwen3.5-4B
       -> RAG：知识卡 + BGE-M3 检索
       -> Qwen3.5-9B：结构化回答生成
       -> 医疗安全检查 + 输出审核
  -> PG：结果/状态事件/对话存档；Redis：会话/进度
  <- JSON 或 SSE 响应
```

### 0.4 脚本分类总览
| 类别 | 脚本 |
|---|---|
| 服务入口 | `app/main.py`、`app/guard_server.py`、`app/vision_server.py` |
| 启动编排 | `start_*.sh`、`stop_all.sh`、`status_all.sh` |
| 依赖/MQ | `download_models.sh`、`rocketmq_consult.sh` |
| 冒烟自检 | `smoke_test.sh`、`smoke_consult.py`、`smoke_vision_gateway.py`、`verify_deepseek_contract.py`、`warmup_vision.py`、`enable_redis_auth.py` |
| RAG 验证 | `validate_rag_assets.py`、`evaluate_rag_shadow.py`、`compare_suite.py` |
| 批量/回归 | `batch_test.py`、`run_full_suite.py`、`reproduce_rollbacks.py` |
| 性能压测 | `benchmark.py`、`load_matrix.py`、`run_v73_matrix.sh`、`capture_v73_metrics.sh` |
| 验收 | `autodl_acceptance.py`、`synthetic_autodl_acceptance.sh` |
| 查询工具 | `query_dialogue.py` |

---

## 1. 服务入口脚本

### 1.1 `app/main.py` —— Pet Consult API 入口
- **定位**：FastAPI 问诊服务工厂与生命周期容器。
- **启动方式**：`uvicorn app.main:create_app --factory --port 8100`（注意：不提供模块级 `app` 实例）。
- **运行逻辑**：
  1. `get_settings()` 读取环境配置 → 缺 `APP_ENV` 即让 `Settings` 构造失败。
  2. `validate_runtime_settings()`：生产禁任何 `MOCK_*`、只监听本机、禁 Admin API、强制 `REDIS_PASSWORD`、本地模型需 `KNOWLEDGE_API_BASE_URL`、DeepSeek 需契约验证；非生产禁监听公网。
  3. `setup_logging()` 配置日志。
  4. `lifespan`：首启动 `Container.startup()`（装配各服务、连接 Redis/PG、启动 Worker），关闭时 `shutdown()`。
  5. 挂载 `RequestContextMiddleware`、异常处理器、`api_router`。
- **产出**：FastAPI 应用，路由含 `/api/v1/consult`、`/api/v1/consult/stream`、`/health/*`、`/api/v1/conversations`、`/api/v1/admin`、限流端点。
- **依赖**：`app/api/router`、`app/core/*`、`app/core/dependencies.Container`。

### 1.2 `app/guard_server.py` —— Qwen3Guard 审核服务
- **定位**：独立的内容审核 HTTP 服务（`/v1/moderate`），加载 Qwen3Guard-Gen-0.6B。
- **启动**：`uvicorn app.guard_server:create_app --factory --port 8103`。
- **运行逻辑**：
  1. 环境变量解析：`GUARD_MODEL_PATH`（默认 `/root/autodl-tmp/models/Qwen3Guard-Gen-0.6B`）、`GUARD_MAX_NEW_TOKENS`、`GUARD_MAX_QUEUE_SIZE`(16)、`GUARD_REQUEST_TIMEOUT_SECONDS`(1.25)、`GUARD_WORKERS`(1)。
  2. `GuardRuntime.start()`：懒加载模型（无 CUDA 也可 import），启动有界 `asyncio.Queue` + 单/多 worker。
  3. 请求进入 `submit()`：`put_nowait` 入队（队满抛 `GuardOverloaded`），`wait_for` 等待结果（超时抛 `TimeoutError`）。
  4. worker 推理 → `parse_guard_output()` 用正则解析 `Safety/Categories` → `moderation_response()` 生成判级。
  5. 任何推理异常 → 返回保守 `blocked=True`（Review），不静默放行。
- **端点**：`GET /health`（未就绪 503）、`POST /v1/moderate`。
- **产出**：`{blocked, verdict, categories, parse_ok, scene, request_id}`。

### 1.3 `app/vision_server.py` —— VisionGateway 服务
- **定位**：有界代理，转发到共享 vLLM OpenAI 端点（不加载模型权重）。
- **启动**：`uvicorn app.vision_server:create_app --factory --port 8102`。
- **运行逻辑**：
  1. 环境变量：`VLLM_BASE_URL`（默认 `http://127.0.0.1:8101`）、`VLLM_MODEL_NAME`、`VISION_CONCURRENCY`(4)、`VISION_MAX_QUEUE_SIZE`(12)、`VISION_MAX_REQUEST_SECONDS`(45)。
  2. `VisionRuntime.start()`：创建 `httpx.AsyncClient`。
  3. `submit()`：`_pending` 达 `max_queue_size` → 抛 `VisionOverloaded`；信号量限并发；用 `asyncio.timeout` 限制总时长。
  4. 构造请求：`priority`、`max_tokens`、`temperature=0`、`chat_template_kwargs={"enable_thinking": False}`（关闭思考流）、`guided_json`（简化 schema）。
  5. 解析 `choices[0].message.content`，返回 `{content, queue_wait_ms, inference_ms, total_ms, request_id}`。
- **端点**：`GET /health`（探测 vLLM `/v1/models`）、`POST /v1/vision`。
- **依赖**：上游 vLLM 视觉模型。

---

## 2. 启动编排脚本（start / stop / status）

### 2.1 `scripts/start_all.sh` —— 一键启动总入口
- **定位**：AutoDL 原生一键启动（无需 Docker）。
- **运行流程**：
  1. 校验 `GUARD_MODE`∈{off,shadow,enforce}，准备 `runtime/` 目录。
  2. `[1/5]` 启动 Redis（`start_redis.sh`）。
  3. `[2/5]` 若 `GUARD_MODE!=off` 启动 Guard supervisor，等待 `:8103/health`。
  4. `[3/5]` 启动 vision supervisor（vLLM + VisionGateway），等待 `:8101/v1/models` 与 `:8102/health`；`VISION_WARMUP=1` 时运行 `warmup_vision.py`。
  5. `[4/5]` 启动 API（`start_api.sh`），等待 `:18100/health/live` 与 `/ready`。
  6. `[5/5]` `START_NGINX=1` 时启动 Nginx，最后 `status_all.sh`。
- **产出**：就绪输出或退出；依赖 `curl`、`supervisorctl`。
- **参数**：`GUARD_MODE`、`START_NGINX`、`VISION_WARMUP`、`SUPERVISORCTL_BIN`。

### 2.2 `scripts/start_api.sh`
- **定位**：启动 FastAPI（`app.main:create_app`，worker=1）。
- **流程**：定位项目根 → 校验 `.venv/bin/python` → `exec uvicorn app.main:create_app --factory --host 127.0.0.1 --port ${APP_PORT:-8100}`。
- **依赖**：`.venv`、环境配置齐全。

### 2.3 `scripts/start_guard.sh`
- **定位**：启动 Guard 服务（单 worker）。
- **流程**：选 Python（`GUARD_PYTHON`→conda→`.venv`）→ `uvicorn app.guard_server:create_app --host ${GUARD_HOST:-127.0.0.1} --port ${GUARD_PORT:-8103}`。
- **依赖**：Guard 模型路径。

### 2.4 `scripts/start_vision.sh`
- **定位**：启动 VisionGateway（单 worker）。
- **流程**：端口 `:8102` 占用则跳过；否则 `uvicorn app.vision_server:create_app --host ${VISION_HOST:-127.0.0.1} --port ${VISION_PORT:-8102}`。
- **特点**：端口占用保护，避免重复启动。

### 2.5 `scripts/start_vllm.sh`
- **定位**：通用 vLLM `serve` 启动。
- **流程**：按环境变量组装 `vllm serve <MODEL_PATH> --host --port --served-model-name --max-model-len --gpu-memory-utilization --max-num-seqs --scheduling-policy priority --limit-mm-per-prompt image:1`；`VLLM_ENFORCE_EAGER=1` 加 `--enforce-eager`；`VLLM_QUANTIZATION` 加 `--quantization`；`VLLM_SPECULATIVE_CONFIG` 加投机解码。
- **关键**：强制 `VLLM_USE_FLASHINFER_SAMPLER=0`（SM120 依赖）。

### 2.6 `scripts/start_models.sh` —— 本地模型栈（单卡共存）
- **定位**：单卡 RTX 5090 一键启动 4B 视觉 + VisionGateway + 9B 生成 FP8。
- **流程**：
  1. 端口预检 `8101/8102/8001` 空闲，否则退出。
  2. `[1/3]` 启动 4B vLLM（`:8101`, 显存 0.37）+ VisionGateway（`:8102`）。
  3. `[2/3]` 轮询等 4B 就绪（避免显存探测竞争）。
  4. `[3/3]` 启动 9B FP8 vLLM（`:8001`, 显存 0.55, maxlen 4096, max-num-seqs 8）。
- **依赖**：vllm 0.27.1、torch 2.13.0+cu130、模型权重。

### 2.7 `scripts/start_redis.sh`
- **定位**：启动本地 Redis 并开启认证。
- **流程**：校验 `redis-server`/`python` → `redis-server --bind 127.0.0.1 --protected-mode yes --appendonly yes --dir runtime/redis --daemonize yes` → 循环调用 `enable_redis_auth.py`（最多 10 次）→ 成功输出 `redis ready`。

### 2.8 `scripts/start_nginx.sh`
- **定位**：启动/Reload 对外 Nginx 网关。
- **流程**：`nginx -t` 校验配置 → 已有 PID 则 `reload`，否则 `start` → 轮询网关健康 `6006/6008 /health/live`。
- **依赖**：Nginx 二进制与 `configs/nginx.conf`。

### 2.9 `scripts/start_guard_supervisor.sh`
- **定位**：以 supervisord 方式拉起 Guard（用于进程守护）。
- **流程**：选 `supervisord` 二进制 → 建 `runtime/guard` → `exec supervisord -c configs/guard-supervisord.conf`。

### 2.10 `scripts/start_vision_supervisor.sh`
- **定位**：以 supervisord 方式拉起 Vision（vLLM + Gateway）。
- **流程**：选 `supervisord` → 建 `runtime/vision` → `exec supervisord -c configs/vision-supervisord.conf`。

### 2.11 `scripts/stop_all.sh` —— 全量停止
- **流程**：
  1. `[1/4]` 停止本项目管理 Nginx（读 PID，`nginx -s quit`）。
  2. `[2/4]` 停止 API（读 `runtime/api.pid`，校验进程属于项目目录才 kill）。
  3. `[3/4]` 停止 Guard（若 `GUARD_MODE!=off`）与 Vision supervisord（`supervisorctl shutdown`），等 PID 退出。
  4. `[4/4]` 停 Redis（`redis.shutdown(save=True)`，先 ping 判断是否已停）。
  5. 轮询端口 `6006/6008/6379/8100/8101/8102(+/8103)` 是否全部关闭。
- **安全**：只停属于项目目录的进程，拒绝误杀。

### 2.12 `scripts/status_all.sh` —— 全量健康检查
- **流程**：校验 `GUARD_MODE` → `supervisorctl status`（Guard 按模式 Skip）→ `curl` 检查 `vLLM :8101`、`Guard :8103`、`VisionGateway :8102`、`API live/ready :18100`；Nginx 运行时再检查 `6006/6008`。
- **输出**：逐项 `PASS/FAIL/SKIP`，任一失败退出非 0。

---

## 3. 依赖与消息队列脚本

### 3.1 `scripts/download_models.sh`
- **定位**：首次部署幂等下载模型（ModelScope 国内源）。
- **流程**：`MODEL_DIR`（默认 `/data/models`）已存在且非空 → skip；否则 `modelscope download`。依次下载：`Qwen3.5-4B`、`Qwen3.5-9B`、`bge-m3`、`Qwen3Guard-Gen-0.6B`。最后 `du -sh` 汇总。
- **特点**：可重跑，跳过已下载。

### 3.2 `scripts/rocketmq_consult.sh` —— 独立 RocketMQ 实例
- **定位**：问诊系统独立 MQ 实例（namesrv `:9877` / broker `:10912`，集群 `pet-consult`），与审核系统 `:9876/:10911` 完全隔离。
- **用法**：`bash scripts/rocketmq_consult.sh {start|stop|restart|status}`。
- **流程**：`start_namesrv`/`start_broker` 用 `nohup env JAVA_OPT_EXT mqnamesrv/mqbroker`，写 PID，`wait_port` 等就绪；`stop_one` 先 kill 再兜底 `pkill -f rocketmq-consult-*.conf`（不误杀审核实例）；`status` 打印进程与端口。

---

## 4. 冒烟与自检脚本

### 4.1 `scripts/smoke_test.sh`
- **定位**：部署后三场景 + 急症 + 流式 + 任务表自检（curl 直接调用）。
- **流程**：`date/curl` 逐项断言：直答(猫藓 success+answer)、追问(狗拉稀 provisional)、详细症状(normal)、急症(呼吸困难 success)、流式 `final` 事件存在。累计 `PASS/FAIL` 统计。
- **参数**：`API`（默认 `http://127.0.0.1:18100`）。

### 4.2 `scripts/smoke_consult.py`
- **定位**：问诊链路冒烟（`/api/v1/consult`），3 用例：图+描述、急症、有图无文字。
- **流程**：`post()` 每个用例独立 `conversation_id`（避免历史干扰）、`trust_env=False`（绕过本地代理）；`response_problems()` 校验 `status/answer_mode/answer/disclaimer/risk_level`，急症查 `vet_recommendation`，provisional 查 `follow_up_questions`；逐条打印 `PASS/FAIL`。
- **参数**：`--base`、`--image`、`--token`。
- **产出**：有失败返回 1。

### 4.3 `scripts/smoke_vision_gateway.py`
- **定位**：共享 VisionGateway 冒烟（走 Gateway 不直连 vLLM）。
- **流程**：按图片初始化 `ProcessedImage`（校验格式 JPEG/PNG/WEBP）→ `VisionGatewayClient.analyze()` → 逐图打印 `quality/species/parts/obs/red_flags/conf`，统计非 `unusable` 占比。
- **参数**：图片路径 1-3 张；预期 `ok==len(findings)`。

### 4.4 `scripts/verify_deepseek_contract.py`
- **定位**：DeepSeek 官方 API 契约验证（鉴权、结构化输出、医疗安全字段、thinking 配置）。
- **流程**：`run_checks` 5 项：① 配置完整 ② 真实鉴权调用成功 ③ 结构化 JSON 可解析 ④ 医疗安全字段完整 ⑤ thinking 固定为 disabled/enabled。通过后 `--confirm` 写契约标记，失败 `invalidate()`。
- **参数**：`--question`、`--confirm`。
- **依赖**：`KNOWLEDGE_PROVIDER=deepseek_official`。

### 4.5 `scripts/warmup_vision.py`
- **定位**：API 启动前用生产请求形状预热 VisionGateway。
- **流程**：加载代表性图片或生成稳定尺寸 855×663 占位图 → `VisionGatewayClient.analyze()` → 校验返回 1 条 findings 并打印 quality。
- **参数**：`--image`、`--timeout`（默认读环境 60s）。

### 4.6 `scripts/enable_redis_auth.py`
- **定位**：从 `REDIS_PASSWORD` 开启 Redis 认证。
- **流程**：读取 `Settings.redis_password`（空则报错）→ 未认证连接 `dbsize()`+`save()` → `config_set requirepass` → 用认证连接校验 `ping()` 与 `dbsize()` 相等。
- **产出**：成功打印 `redis_auth_enabled=yes keys=N`。

---

## 5. RAG 验证与评估脚本

### 5.1 `scripts/validate_rag_assets.py`
- **定位**：只读验收 RAG 资产（默认 `assets/rag/v1_8`），不改写输入。
- **流程**：`RagAssetLoader.load()` 生成报告 → 统计卡片数、待审核数、可上线数、错误；加载 `V14EmergencyShadowMatcher` 统计急症规则；`--require-production` 时要求全部可上线。
- **产出**：JSON 报告（含 `shadow_ready`/`production_ready`/`errors`）。
- **参数**：`--assets`、`--require-production`；退出码按 `shadow_ready && !errors`。

### 5.2 `scripts/evaluate_rag_shadow.py`
- **定位**：评估 v1.4+ 词法 Shadow 检索器在种子与安全探针上的表现。
- **流程**：加载资产 → `ShadowRetriever(top_k=4, threshold)` → 跑种子集算 `Recall@1`、`Recall@4`、`MRR@4`、物种错配率 → `REJECTION_PROBES` 判 `insufficient` 拒绝率 → 急症规则触发召回 / 否定假阳性 / 物种假阳性。
- **参数**：`--assets`、`--threshold`（默认 0.24）。
- **产出**：JSON 报告；`shadow_ready` 为假返回 1。

### 5.3 `scripts/compare_suite.py`
- **定位**：shadow vs grounded 对比报告（生成 Markdown）。
- **流程**：读取两份 JSONL（`--a` shadow，`--b` grounded）按 id 对齐 → 逐条生成对比表 + 双侧回答引用 → 写 `--out`。
- **参数**：`--a`、`--b`、`--out`（默认 `suite_compare_report.md`）。

---

## 6. 批量与回归测试脚本

### 6.1 `scripts/batch_test.py`
- **定位**：读 JSONL 问题清单逐条发请求，输出结果表 + 汇总。
- **流程**：读 `--file` 每行 JSON：`_post_one()` 支持普通/流式端点；统计耗时、状态、模式、追问数、风险、回答长度；PASS 判据为 `mode==expect`（`fast` 时 `ms<1000`）；串行 `sleep 0.3`；最后打印汇总。
- **参数**：`--file`、`--api`、`--stream`。

### 6.2 `scripts/run_full_suite.py`
- **定位**：全功能跑测（shadow/grounded 两轮 + 功能覆盖）。
- **流程**：读 `testdata/full_suite_questions.jsonl` → `run_question()` 逐个 `POST /api/v1/consult`（含可选图片）→ 收集 `{id, ms, status, mode, risk, followups, answer, error}` → 写 `--out` JSONL。
- **参数**：`--mode`、`--file`、`--out`、`--api`；通常配合 `compare_suite.py` 使用。

### 6.3 `scripts/reproduce_rollbacks.py`
- **定位**：复现固定模板回落题目，抓 `medical_review` 违规明细。
- **流程**：对固定题目列表逐个 `POST` 问诊 → 判断是否模板回落（`规则评估未发现` 且 <250 字）→ 从 `runtime/api.log` 提取该 `request_id` 的 `medical_review/safety_rewrite/generate` 事件 → 写 `/tmp/rollback_analysis.txt`。
- **特点**：本地调试工具，日志路径硬编码。

---

## 7. 性能压测脚本

### 7.1 `scripts/benchmark.py`
- **定位**：并发压测（单图/三图 P95 与并发）。
- **流程**：`concurrency` 分批 `asyncio.gather` 并发 `POST /api/v1/consult`；每请求独立 `conversation_id`（避免串行锁失真）；统计成功率、状态分布、P50/P95/P99。
- **参数**：`--base`、`--concurrency`、`--n`、`--image`、`--token`。
- **注意**：`V1.1 P0-5` 要求独立会话，否则退化为串行。

### 7.2 `scripts/load_matrix.py`
- **定位**：v7.3 文本/图片阶梯压测，区分冷缓存/热缓存/禁用缓存与工作负载。
- **流程**：
  1. `validate_args` 校验并发、图片数、缓存模式。
  2. `run_round`：`--warm` 且带图时先 `warmup_requests` 预热（失败即停）；再按并发批次 `gather` 发送。
  3. 每请求独立 `conversation_id`/`X-Request-Id`/`Idempotency-Key`；`build_request_text` 在 cold+图片时追加 `[视觉压测样本 <reqid>]` 避免热缓存。
  4. `extract_response` 解析 SSE 阶段耗时（`vision_completed`/`answer_generated`/`medical_review_completed`/`output_review_completed`）与视觉网关指标。
  5. 只统计 2xx 且业务 status 非 error 为成功（503 不进分位数）。
- **参数**：`--workload{text|single-image|triple-image|mixed}`、`--cache-mode{cold|warm|disabled}`、`--concurrency`、`--per`、`--image-percent`、`--json-out`、`--no-stage-events`。
- **产出**：控制台表格 + `--json-out` 逐请求结果（含 `by_kind` 分类 + 分位数 + 吞吐/min）。

### 7.3 `scripts/run_v73_matrix.sh`
- **定位**：一键运行 v7.3 压测矩阵（调用 `load_matrix.py` 多个组合）。
- **流程**：校验三张图片存在 → 建输出目录 → 依次跑：text 对照、cold 单图、cold 三图、warm 单图、mixed(10/20%)、mixed 50% → 写各自 JSON。
- **用法**：`bash scripts/run_v73_matrix.sh a.jpg b.jpg c.jpg [base] [outdir]`。

### 7.4 `scripts/capture_v73_metrics.sh`
- **定位**：压测同窗采集 GPU/容器/vLLM 指标。
- **流程**：`docker compose ps/config` 快照 → 对 4 个服务导出白名单环境变量与命令 → 逐秒写 `nvidia-smi.csv`、`docker-stats.csv`、`vllm-metrics.promlog`（`num_requests_running/waiting`、`gpu_cache_usage_perc`、token 计数）。
- **参数**：`$1` 时长（默认 300s）、`$2` 间隔（默认 2s）、`$3` 输出目录。

---

## 8. 验收脚本

### 8.1 `scripts/autodl_acceptance.py`
- **定位**：AutoDL 真实链路验收（配置、契约、依赖健康、问诊冒烟）。
- **流程**：
  1. `validate_settings`：`APP_ENV=test`、全部 `MOCK_*=false`、`GUARD_MODE` 合法、JWT/日志/Redis/知识 Key 已配、DeepSeek 契约有效。
  2. `check_tcp`：Redis、VisionGateway、Guard(若强制) 端口可达。
  3. `check_health`：VisionGateway/Guard/API live/ready；ready 依赖全绿。
  4. `run_consult`：带 `--image` 与 `--token` 发真实问诊，断言 success+answer+disclaimer+risk+knowledgedegraded=false 且无 redis/vision 降级旗标。
- **参数**：`--base`、`--token`(或 `AUTODL_TEST_JWT`)、`--image`、`--text`。
- **产出**：逐项 `PASS/FAIL`，失败项计数返回 1。

### 8.2 `scripts/synthetic_autodl_acceptance.sh`
- **定位**：无隐私合成数据的自动化验收（内嵌生成合成宠物图）。
- **流程**：
  1. 用 PIL 生成 256×256 卡通宠物 PNG。
  2. 生成随机 `JWT_SIGNING_SECRET`/`LOG_HASH_SECRET`，`start_api.sh` 后台启动，`trap cleanup` 退出清理。
  3. 轮询 `:18100/health/live` 就绪。
  4. 用 Python 生成 HS256 JWT（`scope=["pet:consult"]`）。
  5. 调 `autodl_acceptance.py --base --image <合成图>`；失败则打印 API 日志尾部。

---

## 9. 查询工具

### 9.1 `scripts/query_dialogue.py`
- **定位**：`request_id` 穿透查询 / 对话效果评审。
- **流程**：数据源优先 PG `consult_dialogue`（无 PG 回退 `runtime/dialogue.jsonl`）→ 按时间倒序 → 支持 `--id` 单条完整链路、`--keyword` 关键词、`--recent N` 最近摘要；`_fmt_one()` 打印请求/用户/命中卡片/RAG/降级/追问/耗时/回答。
- **参数**：`--id`、`--keyword`、`--recent`、`--limit`、`--db`、`--jsonl`。

---

## 10. 通用约定与安全边界

- **幂等与独立会话**：压测/冒烟一律使用独立 `conversation_id`/`request_id`/`Idempotency-Key`，避免并发与串行锁干扰指标。
- **命中真实链路**：`autodl_acceptance.py`、`synthetic_autodl_acceptance.sh` 要求全真实环境（无 Mock、需 JWT）。
- **安全边界**：文档/脚本只提取可见事实；急症判断先于模型；依赖失败走固定安全模板；生产禁 Mock、禁监听公网、签名密钥强制配置。
- **只读原则**：`validate_rag_assets.py` 明确"不改写输入"；压测/冒烟均为请求类脚本，不污染源码。
