# Pet Consult v7.3 当前架构

更新日期：2026-08-25

## 总体链路

```text
小程序/业务客户端
  -> 业务网关与适配层（公网 :19000，/api/biz/consult）
  -> Pet Consult API（宿主机 127.0.0.1:18100，容器 :8100）
       -> 鉴权、限流、幂等、输入校验
       -> 急症规则预判
       -> PostgreSQL：consult_task + consult_outbox 同事务登记
       -> RocketMQ：任务发布与削峰
       -> API 容器内 Worker 并发消费
            -> 图片处理 -> VisionGateway -> Qwen3.5-4B/vLLM
            -> RAG：知识卡 + BGE-M3 混合检索
            -> Qwen3.5-9B/vLLM：结构化回答生成
            -> 医疗安全检查与输出审核
       -> PostgreSQL：结果、状态事件、对话存档
       -> Redis：会话、缓存、并发闸门、SSE 进度
  <- JSON 或 SSE 最终响应
```

业务网关与小程序代码不属于本源码包；本仓库从内部
`/api/v1/consult` 与 `/api/v1/consult/stream` 开始负责问诊处理。

## 运行组件

| Compose 服务 | 职责 | 宿主机暴露 |
|---|---|---|
| `consult-api-1` | HTTP/SSE、任务登记、Worker、问诊编排 | `127.0.0.1:18100` |
| `consult-vllm-text` | Qwen3.5-9B 文本生成 | 仅容器网络 `:8002` |
| `consult-vllm-vision` | Qwen3.5-4B 图片理解 | 仅容器网络 `:8001` |
| `consult-vision-gateway` | `/v1/vision` 到 vLLM Chat 协议转换、并发队列与缓存 | 仅容器网络 `:8102` |
| `postgresql` | 任务、Outbox、事件、结果和对话存档 | 仅本机/容器网络 |
| `redis` | 会话、缓存、幂等辅助、并发闸门和 SSE 进度 | 仅容器网络 |
| `rocketmq-namesrv` | MQ 服务发现 | `127.0.0.1:9877` |
| `rocketmq-broker` | 问诊任务消息队列 | `127.0.0.1:10912` |

模型容器和视觉网关属于 Compose 的 `gpu` profile，完整启动必须使用
`docker compose --profile gpu up -d`。

## 请求处理阶段

1. `app/api/consult.py` 解析 multipart、图片和多宠信息。
2. `ConsultCommand` 确定本次问诊的当前宠物。
3. 规则层先执行急症预判；急症不依赖模型才能返回安全处置。
4. 普通请求写入 PostgreSQL 任务表与 Outbox，再发布 RocketMQ。
5. Worker 从持久化载荷恢复请求，执行视觉、RAG、生成和安全检查。
6. 结构化结果写回 PostgreSQL；同步接口有界等待结果，SSE 接口转发阶段事件。
7. 超时返回可重试错误，但知识模型请求一旦已经超时不会在同一问诊内再次重试，避免放大拥塞。

## 文本与图片模型分工

- Qwen3.5-4B 只负责图片事实提取，不直接给最终诊断或护理结论。
- Qwen3.5-9B 结合用户文本、宠物信息、视觉发现和 RAG 证据组织回答。
- 急症等级、药物边界和输出安全由规则/安全模块二次约束，不能只依赖模型判断。
- RAG 有可靠证据时向模型注入知识卡；证据不足时允许 9B 使用通用宠物健康知识给出审慎回答，并明确不确定性。

## 数据可靠性

- `consult_task` 与 `consult_outbox` 同事务写入，避免任务已登记但消息永久丢失。
- Outbox 扫描器负责失败补发；MQ 消息保留 `task_kind` 和 `image_count` 兼容默认值。
- 任务状态通过 `consult_task_event` 留痕。
- 数据库迁移可重复执行，回滚应用时不要求删除 v7.3 新增列。
- 原始图片只在任务执行期间写入临时文件；任务载荷保存图片元信息而不是长期保存原图。

## 安全边界

- 对外部署应由业务网关完成认证与访问控制；内部 `AUTH_SKIP=true` 仅适用于受控内网链路。
- 生产密钥、模型权重、数据库数据和 Docker 数据卷不进入源码包。
- 回答只提供健康信息与就医建议，不能替代兽医诊断。
- 禁止模型直接给出未经规则允许的药物剂量。
