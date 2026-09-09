# pet-consult

宠物问诊算力侧 API。服务负责症状信息整理、图片事实提取、风险分级、护理观察建议和就医建议，不替代执业兽医诊断。

更新日期：2026-08-25

## 当前状态

| 能力 | 当前实现 |
|---|---|
| API | FastAPI；普通 JSON 响应和 SSE 流式响应 |
| 图片 | 最多 3 张、单张最多 5 MB；本地 Qwen3.5-4B 经 VisionGateway 分析 |
| 问诊生成 | LocalOpenAIAdapter（本地 Qwen3.5-9B FP8，json_schema 结构化输出）；可切 DeepSeek 过渡 |
| RAG | v1_8 知识卡与急症规则；当前本地链路使用 grounded，证据不足由 9B 审慎回答 |
| 多宠物 | 支持数组、`pet_ref`，未指定时按正文名称或猫/狗物种自动选择 |
| 追问 | fast 直答（简单问答）+ provisional（信息不足追问）+ 模型自决追问 |
| 队列化 | PG 任务表 + Outbox + RocketMQ 发布（pyrocketmq）+ 可配置 Worker 消费 |
| 安全 | 文字急症前置、风险引擎、医疗检查、固定安全模板、可选 Qwen3Guard |
| 会话 | Redis 会话、幂等、限流，按环境和租户隔离 |
| 总时限 | 单次问诊绝对 deadline 120 秒 |

## 快速启动

本地 Mock 开发：

```powershell
Copy-Item .env.example .env
.\\.venv\\Scripts\\python.exe -m uvicorn app.main:create_app --factory --port 8100
```

运行测试与 RAG 校验：

```powershell
.\\.venv\\Scripts\\python.exe -m pytest -q
.\\.venv\\Scripts\\python.exe scripts\\validate_rag_assets.py
.\\.venv\\Scripts\\python.exe scripts\\evaluate_rag_shadow.py
```

生产 Compose 使用项目根目录的 `compose.yaml`（依赖安装见 `requirements.lock`）：

```bash
bash stack.sh init
vi .env.docker
bash stack.sh config
bash stack.sh up
```

`compose.yaml` 是当前规范配置，服务器通过 `.env` 将宿主机 API 端口设置为 `18100`。完整模型链路必须使用 `--profile gpu`。

## Docker 部署

详见 `README_DEPLOY.md` 与《Docker 部署验证报告》。关键点：

- 依赖全部锁定在 `requirements.lock`（实测版本），构建时自动安装；
- compose 含 8 服务：postgresql/redis/vllm-vision/vision-gateway/vllm-text/rocketmq×2/api；
- VisionGateway 是独立服务（API 调 /v1/vision 契约，vLLM 不提供）；
- vLLM 镜像必须 v0.27.1+（SM120 必需）；
- 本地 9B 方案：`docker compose --profile gpu up -d`。

## 文档

- [完整交接手册](HANDOFF-v7.3-COMPLETE-20260825.md)
- [当前架构](docs/ARCHITECTURE.md)
- [API 契约](docs/API.md)（含多宠物协议）
- [后端联调](docs/INTEGRATION.md)
- [部署说明](docs/DEPLOYMENT.md)
- [端口表](docs/PORTS.md)
- [RAG v1_8 状态](docs/RAG.md)

## 安全边界

- 不输出具体药物剂量，不把模型输出描述为确诊。
- 急症判断优先于 Vision、RAG 和生成模型；依赖失败时使用固定安全模板。
- 图片只提取可见事实，不能据此排除严重疾病。
- 生产环境禁止 Mock；若内部链路使用 `AUTH_SKIP=true`，必须由公网业务网关完成认证和网络隔离。
- 当前 RAG 全部记录仍为测试资产，不能作为已完成临床审核的知识库。
