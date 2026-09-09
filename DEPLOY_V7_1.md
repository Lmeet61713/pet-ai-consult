# v7.1 热修复部署与回滚

运行目录：

```bash
ROOT="/home/x17/ai_server/pet_ai/apps/delivery_opt_20260821/runtime/pet-consult"
PATCH="/home/x17/ai_server/pet_ai/apps/pet-consult-response-quality-v7.1-hotfix-20260822.tar.gz"

cd "$ROOT"
```

## 1. 校验并备份

```bash
sha256sum "$PATCH"

BACKUP="/home/x17/ai_server/pet_ai/apps/pet-consult-before-v7.1-$(date +%Y%m%d_%H%M%S).tar.gz"

tar -czf "$BACKUP" \
  app/agent/completeness_checker.py \
  app/agent/consult_agent.py \
  app/rag/retriever.py \
  app/rag/hybrid_retriever.py \
  app/services/medical_safety_service.py \
  VERSION \
  compose.yaml

echo "备份：$BACKUP"
```

## 2. 解压和语法检查

```bash
tar -xzf "$PATCH" -C "$ROOT"

python3 -m py_compile \
  app/agent/completeness_checker.py \
  app/agent/consult_agent.py \
  app/rag/retriever.py \
  app/rag/hybrid_retriever.py \
  app/services/medical_safety_service.py

cat VERSION
```

预期版本：`20260822-response-quality-v7.1`。

## 3. 构建 API 增量镜像

只复制 API 代码，不重装依赖，不重建 vLLM。

```bash
docker build \
  --no-cache \
  --build-arg BASE_IMAGE=pet-consult-api:20260822-response-guard-v7 \
  -t pet-consult-api:20260822-response-quality-v7.1 \
  -f - . <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

COPY app /app/app
COPY VERSION /app/VERSION
DOCKERFILE
```

## 4. 切换 Compose 并只重建 API 容器

```bash
cp -p compose.yaml compose.yaml.before-v7.1

sed -i \
  's#^    image: pet-consult-api:.*$#    image: pet-consult-api:20260822-response-quality-v7.1#' \
  compose.yaml

grep -n -A3 '^  consult-api-1:$' compose.yaml

docker compose --env-file .env --profile gpu config --quiet &&
docker compose \
  --env-file .env \
  --profile gpu \
  up -d \
  --no-deps \
  --no-build \
  --force-recreate \
  --wait \
  --wait-timeout 180 \
  consult-api-1
```

## 5. 验证运行版本

```bash
docker inspect \
  pet-consult-v2-consult-api-1-1 \
  --format '镜像={{.Config.Image}} 健康={{.State.Health.Status}}'

docker exec \
  pet-consult-v2-consult-api-1-1 \
  cat /app/VERSION

curl -fsS http://127.0.0.1:18100/health/live
echo
```

预期：镜像为 `pet-consult-api:20260822-response-quality-v7.1`、状态为 `healthy`、版本文件为 `20260822-response-quality-v7.1`。

## 6. 容器内关键规则检查

```bash
docker exec -i pet-consult-v2-consult-api-1-1 python - <<'PY'
from types import SimpleNamespace

from app.agent.completeness_checker import CompletenessChecker
from app.rag.retriever import is_cat_chin_specific_query
from app.schemas.pet import PetInfo

state = SimpleNamespace(
    pet_info=PetInfo(species="dog"),
    case_facts=SimpleNamespace(asked_questions=[]),
)
questions = CompletenessChecker._thin_questions(
    state,
    text="狗拉稀怎么办",
    rag_questions=["犬的年龄是多大？疫苗接种是否完成？"],
)

assert "一天大概几次" in questions[0]
assert all("疫苗" not in question for question in questions)
assert CompletenessChecker._is_general_care_question("狗一般多久洗一次澡比较合适")
assert is_cat_chin_specific_query("猫下巴有黑色颗粒，皮肤有结痂，掉毛", "cat")

print("v7.1 关键规则检查通过")
print(questions)
PY
```

## 7. 回滚

```bash
cd "$ROOT"

cp -p compose.yaml.before-v7.1 compose.yaml

docker compose --env-file .env --profile gpu config --quiet &&
docker compose \
  --env-file .env \
  --profile gpu \
  up -d \
  --no-deps \
  --no-build \
  --force-recreate \
  --wait \
  --wait-timeout 180 \
  consult-api-1
```

回滚只切回 v7 API 镜像，不影响文本 vLLM、视觉 vLLM、视觉网关和数据库。
