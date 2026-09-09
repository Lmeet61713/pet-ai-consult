# v7.2 部署、验证与回滚

版本：`20260823-quality-capacity-v7.2`

运行目录：

```bash
ROOT="/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult"
PATCH="/home/x17/ai_server/pet_ai/apps/pet-consult-quality-capacity-v7.2-hotfix-20260823.tar.gz"
cd "$ROOT"
```

## 1. 部署前确认线上实际参数

```bash
docker inspect pet-consult-v2-consult-api-1-1 \
  --format '{{range .Config.Env}}{{println .}}{{end}}' |
grep -E '^(CONSULT_WORKER_COUNT|QUEUE_MAX_PENDING|VISION_MAX_TOKENS)='

docker inspect pet-consult-v2-consult-vllm-text-1 \
  --format '{{range .Config.Cmd}}{{println .}}{{end}}' |
sed -n '/--max-num-seqs/{n;p;}'

docker inspect pet-consult-v2-consult-vllm-vision-1 \
  --format '{{range .Config.Cmd}}{{println .}}{{end}}' |
sed -n '/--max-num-seqs/{n;p;}'
```

`/v1/models` 只能确认模型和健康状态，不能可靠确认 `max-num-seqs`。

## 2. 校验与备份

```bash
sha256sum "$PATCH"

BACKUP="/home/x17/ai_server/pet_ai/apps/pet-consult-before-v7.2-$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$BACKUP" \
  app compose.yaml .env .env.docker VERSION
echo "备份：$BACKUP"
```

## 3. 解压、语法检查和数据库辅助索引

```bash
tar -xzf "$PATCH" -C "$ROOT"

python3 -m py_compile \
  app/agent/consult_agent.py \
  app/services/medical_safety_service.py \
  app/services/image_service.py \
  app/tasks/service.py \
  app/tasks/progress.py \
  app/api/consult.py

cat VERSION
```

预期：`20260823-quality-capacity-v7.2`。

新增索引（可重复执行）：

```bash
docker compose --env-file .env --profile gpu exec -T postgresql sh -lc '
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE INDEX IF NOT EXISTS ix_consult_task_status_updated
ON consult_task (status, updated_at);
SQL
'
```

部署前检查 PG 总连接上限：

```bash
docker compose --env-file .env --profile gpu exec -T postgresql sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SHOW max_connections;"
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM pg_stat_activity;"
'
```

## 4. 构建 v7.2 API 镜像

只基于当前 v7.1 API 镜像复制代码，不重装依赖：

```bash
docker build \
  --no-cache \
  --build-arg BASE_IMAGE=pet-consult-api:20260822-response-quality-v7.1 \
  -t pet-consult-api:20260823-quality-capacity-v7.2 \
  -f - . <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY app /app/app
COPY VERSION /app/VERSION
DOCKERFILE
```

## 5. 第一阶段：先部署代码，保持旧并发回归

第一次切换先保留 Worker 6、活动上限 8、视觉并发 2、视觉输出 512，排除代码回归。

```bash
set_env() {
  key="$1" value="$2" file="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

set_env CONSULT_WORKER_COUNT 6 .env.docker
set_env QUEUE_MAX_PENDING 8 .env.docker
set_env VISION_MAX_TOKENS 512 .env.docker
set_env CONSULT_DB_POOL_SIZE 20 .env.docker
set_env CONSULT_DB_MAX_OVERFLOW 10 .env.docker
set_env CONSULT_DB_POOL_TIMEOUT_SECONDS 10 .env.docker
set_env QUEUE_ACTIVE_STALE_SECONDS 120 .env.docker
set_env CONSULT_SSE_STREAM_TTL_SECONDS 300 .env.docker
set_env CONVERSATION_LOCK_WAIT_SECONDS 20 .env.docker
set_env CONVERSATION_LOCK_LEASE_SECONDS 95 .env.docker
set_env CONSULT_API_IMAGE pet-consult-api:20260823-quality-capacity-v7.2 .env

docker compose --env-file .env --profile gpu config --quiet &&
docker compose \
  --env-file .env \
  --profile gpu \
  up -d --no-deps --no-build --force-recreate \
  --wait --wait-timeout 180 \
  consult-api-1
```

不要重建或重启文本 vLLM、视觉 vLLM和视觉网关。

## 6. 第一阶段功能回归

至少复测：原 32 项、A1-A5、新 SSE 阶段事件、三图问诊。

重点断言：

- 绝育护理没有眼周模板；同一句中的切口观察仍保留。
- 口臭不出现洗澡频率；“口臭期间能不能洗澡”不会误删洗澡主题。
- “狗拉稀怎么办”首句先承接腹泻。
- 幼猫驱虫包含时间节点、个体化计划和异常反应。
- “少量多次易消化食物”最多一次。
- SSE 不再只有心跳。

## 7. 第二阶段：启用 10 Worker、活动上限 12

```bash
set_env CONSULT_WORKER_COUNT 10 .env.docker
set_env QUEUE_MAX_PENDING 12 .env.docker

docker compose --env-file .env --profile gpu config --quiet &&
docker compose \
  --env-file .env \
  --profile gpu \
  up -d --no-deps --no-build --force-recreate \
  --wait --wait-timeout 180 \
  consult-api-1
```

压测要求：超过 12 个活动任务的请求在 1 秒内返回 HTTP 503，并带 `Retry-After: 2`。

## 8. 第三阶段：视觉并发 4

`VISION_CONCURRENCY` 是 Compose 插值变量，必须写入 `.env`，不是只写 `.env.docker`。

```bash
set_env VISION_CONCURRENCY 4 .env
set_env VISION_VLLM_MAX_NUM_SEQS 8 .env

docker compose --env-file .env --profile gpu config --quiet &&
docker compose \
  --env-file .env \
  --profile gpu \
  up -d --no-deps --no-build --force-recreate \
  --wait --wait-timeout 180 \
  consult-vision-gateway
```

视觉 vLLM 保持运行，不重启。

## 9. 视觉输出 512/320 A/B

先保持 `VISION_MAX_TOKENS=512` 完成 image-01/02/03；通过后再测试 320：

```bash
set_env VISION_MAX_TOKENS 320 .env.docker
docker compose --env-file .env --profile gpu up -d \
  --no-deps --no-build --force-recreate --wait --wait-timeout 180 \
  consult-api-1
```

若擦伤、圆形伤口、红旗或多图细节丢失，改为 384；仍有截断则回滚 512。

## 10. 文本 vLLM 8→12（仅实际为 8 时）

如果第 1 步确认线上已经是 16，保持 16，不降低。如果是 8，在维护窗口执行：

```bash
set_env TEXT_VLLM_MAX_NUM_SEQS 12 .env
docker compose --env-file .env --profile gpu config --quiet &&
docker compose --env-file .env --profile gpu up -d \
  --no-deps --no-build --force-recreate \
  --wait --wait-timeout 300 \
  consult-vllm-text
```

这一步会重启文本 vLLM，应在前面 API 功能和 10 Worker 压测完成后单独执行。

## 11. 回滚

```bash
cd "$ROOT"
tar -xzf "$BACKUP" -C "$ROOT"

docker compose --env-file .env --profile gpu config --quiet &&
docker compose --env-file .env --profile gpu up -d \
  --no-deps --no-build --force-recreate \
  --wait --wait-timeout 180 \
  consult-api-1 consult-vision-gateway
```

新增 PG 索引可以保留；Redis Stream 和视觉缓存会按 TTL 自动过期。
