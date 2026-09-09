# 本地生成模型规划

更新日期：2026-08-14

状态：规划中，尚未实现。

## 目标

将当前 DeepSeek 官方 API 生成替换为本地 vLLM，同时保留 RAG、急症规则、医疗检查和固定安全模板。

## 资源约束

- 目标服务器共 8 张 RTX 5090。
- pet-consult 最多只能使用其中 3 张卡，并且可能只能使用每张卡的部分显存。
- Docker 的 GPU 可见性和 vLLM `gpu-memory-utilization` 不是 MIG 式硬隔离；必须避免其他容器同时冲高显存。

## 候选方案

首选验证：

- Qwen3.6-27B-FP8。
- 2 张卡 tensor parallel，初始 `gpu-memory-utilization=0.60-0.65`。
- `max-model-len=8192`、`max-num-seqs=2`、`--language-model-only`。
- 第 3 张卡继续承载现有 Qwen3.5-9B Vision；Guard 和 RAG 小模型需按实测安排。

资源不足回退：

- 复用现有 Qwen3.5-9B 同时承担 Vision 与受限回答生成。
- 代价是生成质量、延迟和并发能力下降。

## 必须实现的代码改动

1. 新增 `LocalVllmAdapter`，不要继续发送 DeepSeek 特有的 `thinking` 请求字段。
2. 使用 OpenAI 兼容 `/v1/chat/completions` 和严格 JSON Schema。
3. 保留 Pydantic 校验、一次安全重写和固定模板兜底。
4. 增加本地模型健康检查、超时、版本日志和回滚开关。
5. 在同一套真实脱敏问题上比较当前 Provider 与本地模型。

## 蒸馏边界

可以评估使用 DeepSeek-V4-Pro 最终结构化输出做受控行为蒸馏，但目标应是回答格式、证据遵循、拒答和安全行为，不应把教师输出当作医学事实来源。

- 教师输入必须携带经过审核的 RAG 事实和独立风险等级。
- 只保存最终结构化答案，不依赖长篇思维链。
- 高风险样本必须人工审核。
- 训练使用原始或 QLoRA 训练权重，完成后再量化为 FP8 部署。
- RAG 继续作为可更新、可引用的知识来源。

