# Pet Consult v7.3 完整源码与运维交接手册

更新日期：2026-08-25  
交付对象：Pet Consult 宠物智能问诊服务  
交付版本：`20260825-image-text-v7.3-rag2-retry1-multipet1`  
交付形态：完整源码包、SHA256 校验文件、本文档

## 1. 交付结论

本次交付源码以 `20260824-image-text-v7.3` 完整版本为基线，合并了当前服务器已经上线的后续修复：

1. RAG 检索不足时允许 Qwen3.5-9B 使用通用宠物健康知识继续回答；
2. 回答正文调整为自然、温和、面向宠物主人的表达；
3. 知识模型请求超时后不在同一次问诊中重复发起，避免超时重试放大拥塞；
4. 多宠列表未传 `pet_ref` 时，可根据正文中的宠物名称或“猫/狗/犬”自动选择当前宠物；
5. 文本 vLLM Compose 配置包含前缀缓存启动参数；
6. 保留 v7.3 文本/图片任务分类、数据库迁移、RocketMQ 消息兼容和视觉网关链路。

最终源码没有合入曾经试验但未进入当前生产镜像的 `perf1` 生成长度限制代码。生产密码、模型权重、数据库内容、Redis 数据和 Docker 数据卷不在源码包中。

## 2. 当前服务器运行信息

| 项目 | 当前信息 |
|---|---|
| 服务器 | `39.145.28.102` |
| SSH 用户/端口 | `x17` / `22` |
| 应用目录 | `/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult` |
| 上传目录 | `/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime` |
| 当前 API 镜像 | `pet-consult-api:20260825-image-text-v7.3-rag2-retry1-multipet1` |
| 内部 API | `http://127.0.0.1:18100` |
| 公网业务入口 | `http://39.145.28.102:19000` |
| 文本模型 | Qwen3.5-9B，vLLM `:8002` |
| 视觉模型 | Qwen3.5-4B，vLLM `:8001` |
| 文本/视觉 GPU | GPU 2 / GPU 3 |

当前镜像标签和运行配置必须以服务器实时输出为最终依据：

```bash
cd /home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult

grep '^CONSULT_API_IMAGE=' .env
docker compose --profile gpu ps
docker compose config --images
```

说明：服务器当前热修镜像是在旧基础镜像上逐文件构建，因此容器内旧
`VERSION` 文件可能仍显示 `20260824-image-text-v7.3`；本次完整源码已经把
`VERSION` 和 Compose 默认镜像标识统一为本次交付版本。完整重建后以新值为准。

## 3. 系统范围与边界

本源码包负责：

- 内部问诊 API 与 SSE；
- 文本、图片和多宠输入解析；
- 急症规则、输入审核、风险判断和输出安全检查；
- PostgreSQL 任务/Outbox/结果持久化；
- RocketMQ 消息发布与消费；
- RAG 检索与 Qwen3.5-9B 回答生成；
- Qwen3.5-4B 图片事实提取和 VisionGateway；
- Redis 会话、缓存、并发闸门和进度事件；
- Docker Compose 服务定义、配置模板、迁移和运维脚本。

本源码包不负责：

- 小程序或移动端页面；
- 公网 `:19000` 业务适配层和 Nginx 主工程；
- 用户登录、业务账号、订单或其他业务系统；
- 模型权重下载和授权；
- 生产密钥、数据库业务数据和服务器系统配置。

公网业务适配层将 `/api/biz/consult` 的 multipart 字段转发到内部 `/api/v1/consult`。交接时必须同时确认外部适配层没有修改 `pet_info`、`pet_ref`、图片字段和请求头。

## 4. 总体架构

```text
小程序/业务客户端
  |
  v
公网 Nginx + 业务适配层 :19000
  |  /api/biz/consult 或 /api/biz/consult/stream
  v
Pet Consult API :18100 -> 容器 consult-api-1:8100
  |-- 鉴权 / 限流 / 幂等 / multipart 校验
  |-- 急症规则预判
  |-- PostgreSQL 同事务写 consult_task + consult_outbox
  |-- Outbox -> RocketMQ -> Worker
  |                    |
  |                    |-- 文本输入 --------------------+
  |                    |                                 |
  |                    |-- 图片 -> VisionGateway         |
  |                                  |                   |
  |                                  v                   |
  |                           Qwen3.5-4B / GPU3           |
  |                                                      v
  |                         RAG 知识卡 + BGE-M3 -> Qwen3.5-9B / GPU2
  |                                                      |
  |                         医疗安全检查 <- 结构化回答 <-+
  |
  |-- PostgreSQL：任务状态、结果、事件、对话存档
  |-- Redis：会话、缓存、并发闸门、SSE 进度
  v
JSON 响应或 SSE final/error
```

### 4.1 Compose 服务

| 服务 | 主要职责 | 暴露方式 |
|---|---|---|
| `consult-api-1` | API、任务登记、Worker、业务编排 | `127.0.0.1:18100 -> 8100` |
| `consult-vllm-text` | Qwen3.5-9B 文本生成 | Compose 内网 `8002` |
| `consult-vllm-vision` | Qwen3.5-4B 图片理解 | Compose 内网 `8001` |
| `consult-vision-gateway` | 视觉协议转换、缓存、并发队列 | Compose 内网 `8102` |
| `postgresql` | 任务、Outbox、事件、结果、对话 | 持久化命名卷 |
| `redis` | 会话、缓存、闸门和 SSE 进度 | 持久化命名卷 |
| `rocketmq-namesrv` | MQ 服务发现 | `127.0.0.1:9877` |
| `rocketmq-broker` | 问诊任务消息队列 | `127.0.0.1:10912` |

`consult-vllm-text`、`consult-vllm-vision` 和 `consult-vision-gateway` 位于 `gpu` profile，完整启动必须带 `--profile gpu`。

## 5. 源码目录说明

```text
app/
  agent/          问诊状态机、完整度判断、回答编排
  api/            HTTP、SSE、健康检查、限流和管理接口
  clients/        文本模型、视觉网关、Redis 等客户端
  core/           配置、依赖装配、异常、鉴权和常量
  image/          图片格式、尺寸、哈希和预处理
  prompts/        回答生成提示词和安全约束
  rag/            词法/向量混合检索与知识卡判断
  repositories/   会话与幂等存储
  safety/         急症、药物和医疗安全规则
  schemas/        请求、响应、宠物和图片模型
  services/       图片、问诊生成、会话和安全业务层
  tasks/          PostgreSQL 任务、Outbox、MQ、Worker 和迁移
assets/           RAG 卡片、规则和检索资产
configs/          RocketMQ 等组件配置
docs/             API、架构、部署、RAG 和集成文档
scripts/          检查、压测、指标和运维辅助脚本
tests/            单元与集成测试
compose.yaml      生产 Compose 服务定义
Dockerfile.api    API/视觉网关共用镜像构建文件
stack.sh          另一套封装入口；现服务器日常运维以 compose 命令为准
```

重点文件：

| 文件 | 说明 |
|---|---|
| `app/api/consult.py` | multipart、JSON/SSE 入口、任务登记 |
| `app/schemas/consult.py` | `ConsultCommand`、多宠选择、响应结构 |
| `app/agent/consult_agent.py` | 主流程、模型异常和超时处理 |
| `app/clients/knowledge_consult_client.py` | Qwen3.5-9B 调用和结构化输出 |
| `app/prompts/consultation_answer_first_v2.py` | 回答风格、医疗边界 |
| `app/rag/retriever.py` | 词面检索与相关性判断 |
| `app/rag/hybrid_retriever.py` | BGE-M3 混合检索和回退 |
| `app/services/image_service.py` | 图片分析、缓存和降级 |
| `app/vision_server.py` | VisionGateway 服务 |
| `app/tasks/models.py` | 任务、Outbox、事件、对话表模型 |
| `app/tasks/migrations.py` | 可重复执行的 v7.3 数据库迁移 |
| `app/tasks/mq.py` | MQ 消息契约 |
| `app/tasks/runner.py` | Worker 从任务载荷恢复问诊命令 |

## 6. 核心处理流程

### 6.1 文本问诊

1. API 校验 `conversation_id`、文本、多宠信息、鉴权和限流。
2. 急症规则先进行不依赖模型的预判。
3. 普通任务原子登记到 PostgreSQL，并由 Outbox 发布到 RocketMQ。
4. Worker 执行输入审核、RAG、风险判断和 Qwen3.5-9B 生成。
5. 医疗安全模块检查回答，随后保存结果。
6. 同步接口在有界时间内等待任务结果；SSE 接口持续发送阶段事件。

### 6.2 图片问诊

1. API 接收最多 3 张图片，校验单张大小、像素和格式。
2. 图片任务设置 `task_kind=image` 和 `image_count`。
3. Worker 调用 VisionGateway；网关负责上游协议转换、缓存和并发队列。
4. Qwen3.5-4B 只输出可观察到的图片事实。
5. Qwen3.5-9B 结合文字、宠物档案、视觉发现和 RAG 生成最终回答。
6. 图片不可用或视觉依赖失败时，系统按配置返回降级信息，不能把模拟观察冒充真实观察。

### 6.3 RAG 与回答策略

- RAG 资产先经过完整性检查，再执行词面和向量混合检索。
- 可靠知识卡作为证据注入回答。
- 没有可靠证据时，不再只返回机械就医提示；9B 可以使用通用宠物健康知识继续回答，但必须使用审慎措辞并给出观察和就医边界。
- 客户端主要展示 `answer`；结构化字段用于组件、审核、存档和二次展示，不应把相同内容重复铺满页面。
- 模型调用超时后不在同一次问诊中再次发起，避免重复占用 GPU；其他明确的瞬时连接错误仍可按现有规则处理。

## 7. 多宠物契约与选择规则

接口字段没有因为本次修复发生变化。`pet_info` 仍是 multipart 中的 JSON 字符串：

单宠：

```json
{"name":"豆豆","species":"dog","age_value":8,"age_unit":"month"}
```

多宠：

```json
[
  {"name":"咪咪","species":"cat","age_value":2,"age_unit":"year"},
  {"name":"豆豆","species":"dog","age_value":8,"age_unit":"month"}
]
```

不要传成 `pets` 字段，不要包成 `{"pets": [...]}`，也不要把 JavaScript 对象直接放入 FormData。调用方必须先执行 `JSON.stringify(pets)`。

当前宠物选择优先级：

1. 显式 `pet_ref`：宠物名称或从 0 开始的数组下标；
2. 正文中唯一出现的宠物名称；
3. 正文中的“猫/狗/犬”与列表中唯一匹配的 `species`；
4. 仍无法确定时，为兼容旧接口回退到数组第一只。

因此，同物种多宠、同时询问多只宠物或文本没有物种线索时，调用方应传 `pet_ref`。显式 `pet_ref` 优先级最高。

## 8. API 契约摘要

### 8.1 内部接口

- `POST /api/v1/consult`：multipart，返回 JSON；
- `POST /api/v1/consult/stream`：multipart，返回 SSE；
- `GET /health/live`：进程存活；
- `GET /health/ready`：Redis、视觉网关和安全依赖就绪；
- `GET/DELETE /api/v1/conversations/{conversation_id}`：会话读取/删除。

问诊表单字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `conversation_id` | 是 | 1–128 位字母、数字、下划线或连字符 |
| `text` | 条件必填 | 与图片至少提供一项，最多 4000 字符 |
| `pet_info` | 否 | 单对象或数组的 JSON 字符串 |
| `pet_ref` | 否 | 名称或数组下标 |
| `images` | 否 | 最多 3 张，单张最多 5 MB |
| `Idempotency-Key` | 否 | Header，避免重复提交 |

### 8.2 公网业务接口

- `POST http://39.145.28.102:19000/api/biz/consult`
- `POST http://39.145.28.102:19000/api/biz/consult/stream`
- `GET http://39.145.28.102:19000/pet-ai/health`

公网接口属于外部业务适配层。适配层应原样转发 multipart 文本字段和图片，并将业务用户映射到内部 `X-User-Id` 或正式 JWT。

### 8.3 主要响应字段

```text
request_id / conversation_id / status / answer_mode / answer
summary / possible_explanations / what_to_do_now / avoid_actions
what_to_monitor / risk_level / risk_flags / vet_recommendation
image_findings / follow_up_questions / disclaimer
knowledge_degraded / retryable / error
```

`status=success` 表示接口完成，不等于兽医确诊。客户端必须保留免责声明和紧急就医提示。

## 9. 数据库与 MQ

### 9.1 主要表

| 表 | 用途 |
|---|---|
| `consult_task` | 请求、状态、输入载荷和最终结果 |
| `consult_outbox` | 待发布 MQ 消息 |
| `consult_task_event` | 状态流转审计 |
| `consult_dialogue` | 问诊效果和对话存档 |

v7.3 为 `consult_task` 增加：

- `task_kind VARCHAR NOT NULL DEFAULT 'text'`；
- `image_count INTEGER NOT NULL DEFAULT 0`；
- `ix_consult_task_kind_status_updated` 索引。

迁移由应用启动自动执行，可重复运行。回滚应用时保留新增字段，不执行删列。

### 9.2 状态机

```text
registered -> queued -> scheduled -> processing
           -> completed | failed | timeout | cancelled | dead_letter
```

### 9.3 MQ 消息

消息包含 `task_id`、`request_id`、`priority`、`fast_path`、`pre_answered`、`task_kind` 和 `image_count`。旧消息缺少后两个字段时按 `text/0` 解析。

## 10. 配置管理

### 10.1 文件职责

- `.env`：Compose 变量、模型路径、GPU、镜像标签；
- `.env.docker`：API 容器运行参数；
- `.env.example`、`.env.docker.example`：无密钥模板；
- `compose.yaml`：服务、网络、卷、健康检查和固定内部地址。

升级完整源码时必须保留服务器现有 `.env` 和 `.env.docker`，禁止用示例文件覆盖生产文件。

### 10.2 关键参数

| 参数 | 作用 |
|---|---|
| `CONSULT_API_IMAGE` | API 运行镜像 |
| `MODEL_DIR` | 宿主机模型根目录 |
| `TEXT_GPU_DEVICE` / `VISION_GPU_DEVICE` | 文本/视觉 GPU |
| `TEXT_VLLM_MAX_NUM_SEQS` | 文本模型最大序列数 |
| `VISION_VLLM_MAX_NUM_SEQS` | 视觉模型最大序列数 |
| `VISION_CONCURRENCY` | 视觉网关同时调用数 |
| `VISION_MAX_QUEUE_SIZE` | 视觉网关等待队列 |
| `CONSULT_WORKER_COUNT` | API 内 Worker 数量 |
| `TEXT_MAX_ACTIVE` | 文本任务准入上限 |
| `IMAGE_MAX_ACTIVE` | 图片任务准入上限 |
| `IMAGE_MAX_ACTIVE_SLOTS` | 按图片数量计费的图片槽位上限 |
| `CONSULT_WAIT_RESULT_TIMEOUT_SECONDS` | 同步接口等待任务结果时间 |
| `CONSULT_SSE_MAX_WAIT_SECONDS` | SSE 最大等待时间 |
| `RAG_MODE` | RAG 工作方式，当前本地链路使用 `grounded` |
| `KNOWLEDGE_MAX_TOKENS` | 0 表示不由应用限制模型生成长度 |

最近一次运行期配置曾使用 Worker 16、文本准入 48、图片准入 12、图片槽位 24、视觉网关并发 6/队列 16；这些是部署配置而不是接口契约，也不代表硬件理论上限。交接时用下列命令读取真实值：

```bash
docker compose exec -T consult-api-1 env \
  | grep -E '^(CONSULT_WORKER_COUNT|TEXT_MAX_ACTIVE|IMAGE_MAX_ACTIVE|IMAGE_MAX_ACTIVE_SLOTS|QUEUE_MAX_PENDING|CONSULT_WAIT_RESULT_TIMEOUT_SECONDS|CONSULT_DB_POOL_SIZE|KNOWLEDGE_MAX_TOKENS)=' \
  | sort

grep -E '^(TEXT_VLLM_MAX_NUM_SEQS|VISION_VLLM_MAX_NUM_SEQS|VISION_CONCURRENCY|VISION_MAX_QUEUE_SIZE|TEXT_GPU_DEVICE|VISION_GPU_DEVICE)=' .env .env.docker
```

## 11. 完整源码部署

### 11.1 上传目录

将源码包、手册和 SHA256 文件上传到：

```text
/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/
```

Windows PowerShell 示例：

```powershell
Set-Location -LiteralPath 'C:\Users\guo\Desktop\优化'

scp -P 22 `
  .\pet-consult-v7.3-final-source-20260825.tar.gz `
  .\HANDOFF-v7.3-COMPLETE-20260825.md `
  .\SHA256SUMS-v7.3-complete-20260825 `
  x17@39.145.28.102:/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/
```

### 11.2 校验、源码和数据库备份

```bash
set -euo pipefail

RUNTIME=/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime
APP=$RUNTIME/pet-consult
STAMP=$(date +%Y%m%d-%H%M%S)

cd "$RUNTIME"
sha256sum -c SHA256SUMS-v7.3-complete-20260825

tar -czf "$RUNTIME/pet-consult-before-final-$STAMP.tar.gz" \
  -C "$(dirname "$APP")" "$(basename "$APP")"

cd "$APP"
docker compose exec -T postgresql \
  pg_dump -U consult -d consult -Fc \
  > "$RUNTIME/consult-before-final-$STAMP.dump"
```

### 11.3 更新源码但保留生产配置

```bash
set -euo pipefail

RUNTIME=/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime
APP=$RUNTIME/pet-consult
STAGE=$RUNTIME/pet-consult-final-stage

mkdir -p "$STAGE"
tar -xzf "$RUNTIME/pet-consult-v7.3-final-source-20260825.tar.gz" -C "$STAGE"

cp "$APP/.env" "$STAGE/.env"
cp "$APP/.env.docker" "$STAGE/.env.docker"

echo "新源码已解压到：$STAGE"
```

先在 `STAGE` 检查配置，不要直接删除现有应用目录。正式切换时可在维护窗口内将旧目录改名备份，再把 `STAGE` 改为 `pet-consult`。

### 11.4 构建并启动

```bash
cd /home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult

docker compose --profile gpu config >/dev/null
docker compose build consult-api-1 consult-vision-gateway
docker compose --profile gpu up -d
```

如果完整重建下载 PyTorch 较慢，应等待镜像层完成。已经存在可用 API 镜像时，也可以先沿用当前镜像标签完成目录切换，再在维护窗口构建新镜像。

## 12. 日常启动、停止和日志

### 12.1 服务器重启后启动

```bash
sudo systemctl start docker
cd /home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult
docker compose --profile gpu up -d
```

### 12.2 查看状态

```bash
docker compose --profile gpu ps
docker compose --profile gpu ps --all
```

### 12.3 查看日志

```bash
docker compose logs -f --tail 200 consult-api-1 consult-vision-gateway
docker compose logs --tail 200 consult-vllm-text
docker compose logs --tail 200 consult-vllm-vision
```

`Ctrl+C` 只退出日志查看，不停止容器。

### 12.4 重启与重新创建

仅重启进程：

```bash
docker compose restart consult-api-1
```

修改 `.env`、`.env.docker`、镜像或 Compose 后：

```bash
docker compose up -d --no-deps --force-recreate consult-api-1
docker compose --profile gpu up -d --no-deps --force-recreate consult-vision-gateway
```

### 12.5 停止

```bash
docker compose --profile gpu stop
```

删除容器和项目网络但保留数据卷：

```bash
docker compose --profile gpu down
```

禁止在生产执行：

```bash
docker compose down -v
```

## 13. 健康检查与验收

```bash
cd /home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult

curl -fsS http://127.0.0.1:18100/health/live
curl -fsS http://127.0.0.1:18100/health/ready

docker compose exec -T consult-vision-gateway \
  python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8102/health", timeout=3).read().decode())'

docker compose exec -T postgresql pg_isready -U consult -d consult
docker compose exec -T redis \
  sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping'
```

容器重建后的最初几秒可能出现 `Recv failure: Connection reset by peer`，只要随后 `/health/ready` 返回 `status=ready` 且容器健康即可。

### 13.1 多宠验证

```bash
RID="handoff_multi_$(date +%s)"

curl --max-time 120 -sS \
  -H 'X-User-Id: handoff-check' \
  -H "Idempotency-Key: $RID" \
  -F "conversation_id=$RID" \
  -F 'text=狗狗拉肚子两天了，应该怎么办？' \
  -F 'pet_info=[{"name":"咪咪","species":"cat","age_value":2,"age_unit":"year"},{"name":"豆豆","species":"dog","age_value":8,"age_unit":"month"}]' \
  http://127.0.0.1:18100/api/v1/consult
```

回答应识别“豆豆/狗狗/8个月”，而不是数组第一只猫。

## 14. 回滚

当前多宠修复前的 API 镜像为：

```text
pet-consult-api:20260825-image-text-v7.3-rag2-retry1
```

回滚 API：

```bash
cd /home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult

sed -i \
  's|^CONSULT_API_IMAGE=.*|CONSULT_API_IMAGE=pet-consult-api:20260825-image-text-v7.3-rag2-retry1|' \
  .env

docker compose up -d --no-deps --force-recreate consult-api-1
curl -fsS http://127.0.0.1:18100/health/ready
```

如需恢复完整目录，使用部署前创建的源码 tar 包。v7.3 数据库新增列保持兼容，不需要删列。数据库恢复属于高风险操作，只有确认数据损坏且已停止写入时才能使用 `pg_restore`。

## 15. 常见故障

### 15.1 容器一直 `starting`

模型首次启动可能加载较久。查看对应模型日志和 GPU：

```bash
docker compose logs -f --tail 200 consult-vllm-text
nvidia-smi
```

不要在模型仍加载时反复重启。

### 15.2 `pet_info 格式不正确`

确认 multipart 的 `pet_info` 是合法 JSON 字符串。JavaScript 必须使用：

```javascript
formData.append("pet_info", JSON.stringify(pets));
```

不能直接 append 对象数组，否则可能变成 `[object Object]`。

### 15.3 多宠回答错对象

检查：

1. `species` 是否为 `cat` 或 `dog`；
2. 问题是否明确出现宠物名称或猫/狗关键词；
3. 业务后端是否错误地固定传了其他宠物的 `pet_ref`；
4. 同物种多宠时是否显式传了正确 `pet_ref`。

### 15.4 任务停在 `registered`

```bash
docker compose logs --since 10m consult-api-1 \
  | grep -E 'outbox_publish_failed|ERROR|Traceback'
```

检查 RocketMQ NameServer/Broker、Outbox 未发布记录和 `app/tasks/mq.py` 消息字段。

### 15.5 `QUEUE_BUSY`

表示分类准入达到当前配置限制，不等同于 GPU 故障。先查看积压、任务耗时、vLLM waiting 和 GPU 利用率，再决定是否调整准入值；不要只提高队列而不确认同步超时和数据库连接池。

### 15.6 `TASK_TIMEOUT` 或 `KNOWLEDGE_CONSULT_UNAVAILABLE`

检查文本 vLLM 的 running/waiting、API 等待时间和模型日志。当前代码不会对已经超时的生成请求立即重复调用，以避免雪崩。客户端应根据 `retryable` 做退避重试，而不是瞬间连续重发。

### 15.7 图片服务不可用

依次检查 `consult-vllm-vision`、VisionGateway 健康接口、GPU 3、`VISION_GATEWAY_BASE_URL` 和视觉网关队列。API 必须访问 VisionGateway，不能直接把 `/v1/vision` 请求发给 vLLM。

### 15.8 修改配置没有生效

修改环境文件后必须 `--force-recreate` 对应服务；`docker restart` 不会重新解析 Compose 环境。

### 15.9 本机没有 `python`

Ubuntu 服务器使用 `python3`；容器内通常使用 `python`。不要因为宿主机缺少 `python` 命令而安装不必要的软件。

## 16. 监控建议

至少持续观察：

- API 请求成功率、HTTP 503、`QUEUE_BUSY` 和任务超时；
- `consult_task` 的 queued/processing 积压；
- Outbox 未发布数量和 MQ 消费异常；
- 文本/视觉 vLLM running、waiting、KV/前缀缓存指标；
- GPU 显存、利用率、温度和进程；
- PostgreSQL 连接池、慢查询和数据卷容量；
- Redis、RocketMQ 和健康检查状态；
- 图片降级、知识生成降级和医疗安全拦截数量。

不要用单次请求延迟或单次 GPU 利用率推断系统容量。运行期准入参数需要结合真实流量结构持续调整。

## 17. 安全与数据要求

- `.env`、`.env.docker`、数据库备份和日志可能包含敏感信息，不提交代码仓库；
- 对外开放前必须确认网关鉴权；内部 `AUTH_SKIP=true` 不能直接暴露公网；
- 禁止在日志中记录完整 JWT、密码、用户隐私或原始图片内容；
- 删除数据卷、恢复数据库、修改生产网络和清理模型目录必须单独审批；
- 回答必须保留“不能替代专业兽医诊断”的边界；
- 急症规则和药物安全规则变更需要专项评审。

## 18. 交接检查表

- [ ] 源码包 SHA256 校验通过；
- [ ] 接收方已保存 `.env` 和 `.env.docker`；
- [ ] PostgreSQL 备份可读取；
- [ ] `docker compose --profile gpu config` 通过；
- [ ] API、文本模型、视觉模型、视觉网关、PostgreSQL、Redis、RocketMQ 均运行；
- [ ] `/health/live` 和 `/health/ready` 正常；
- [ ] 文本问诊正常；
- [ ] 图片问诊正常；
- [ ] 多宠不传 `pet_ref` 时能按正文选择正确物种；
- [ ] SSE 能收到 `final` 或明确的 `error`；
- [ ] 当前镜像标签和回滚镜像已记录；
- [ ] 公网业务适配层字段转发已核对；
- [ ] 生产密钥、模型权重和业务数据由接收方单独接管。

## 19. 交付边界声明

本次交付证明源码已经整理并包含当前已上线的核心修复；它不等同于替接收方完成生产账号、模型授权、灾备演练或长期容量保证。接收方应在自己的维护窗口完成源码包校验、配置复核、备份验证和上线验收。
