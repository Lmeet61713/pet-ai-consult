# pet-consult v6 发布说明

版本：`20260822-followup-v6`

## 本版已包含

- VisionGateway 并发数支持 `VISION_CONCURRENCY` 配置，默认 2。
- 单次最多 3 张问诊图片并发分析，不再逐张串行等待。
- 问诊 Worker 默认 6 个，待处理队列上限默认 8。
- 空知识模型 API Key 不再发送无效 `Authorization` 请求头。
- 图片分析和问诊总超时配置修复。
- 多轮会话自动继承猫/狗物种上下文。
- 信息不足时使用简短、自然且与当前症状相关的追问。
- “状态不好、没精神”等非特异性描述不再误命中具体疾病 RAG 卡片。
- `provisional + RAG 无证据` 时清空无依据病因，并统一安全的就医理由。
- RAG 日志包含 `reason_codes`、物种及物种来源，方便回归和压测分析。

## 安全说明

- 本源码包不包含生产 `.env`、`.env.docker`、密码、Token 或模型权重。
- 部署时从 `.env.example` 和 `.env.docker.example` 创建环境配置。
- 本版只需要重建问诊 API 和（使用视觉并发新代码时）视觉网关镜像；不需要重建 vLLM 模型镜像。

## 核心验收结果

- 公网 `19000` 两轮问诊请求成功。
- 多轮物种从历史档案正确继承。
- 模糊问句返回 `rag_decision=insufficient` 和 `vague_general_query`。
- 无错误 RAG 卡片、无无依据具体病因、追问数量与措辞正常。
