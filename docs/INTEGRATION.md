# 后端联调

更新日期：2026-08-14

## 接入顺序

1. 调用 `/health/live` 和 `/health/ready` 确认服务状态。
2. 使用 Mock 环境联调字段、错误码和 SSE。
3. 切换 `APP_ENV=test`、关闭 Mock，并配置真实 JWT 和依赖。
4. 使用代表性文字与图片逐条验收。
5. 生产部署前执行契约验证、RAG 校验和完整测试。

## Mock 请求

```bash
curl -X POST http://127.0.0.1:18100/api/v1/consult \
  -H 'X-User-Id: integration-user' \
  -H 'Idempotency-Key: demo-001' \
  -F 'conversation_id=conv_001' \
  -F 'text=猫今天食欲下降，精神一般' \
  -F 'pet_info={"species":"cat","age_months":24}'
```

## 业务后端要求

- 前端最多选择 3 张图片；业务后端仍应透传 API 的数量和大小错误。
- 不要把 Token、用户标识或租户标识写入请求体。
- 每次业务操作生成独立 `Idempotency-Key`；不同内容不得复用同一个 Key。
- 对 `retryable=true` 的 503/504 使用有限次数退避重试。
- 对 400、401、409 的非重试错误直接返回业务提示。
- SSE 路由关闭代理缓冲，并处理客户端断开。
- 日志保存 `X-Request-Id`，不要记录原始图片、Token 或完整敏感问诊文本。

## 验收命令

本地：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_consult.py --help
.\.venv\Scripts\python.exe scripts\smoke_vision_gateway.py --help
```

服务器：

```bash
.venv/bin/python scripts/autodl_acceptance.py \
  --base http://127.0.0.1:18100 \
  --token "$AUTODL_TEST_JWT" \
  --image testdata/autodl/pet.png
```

本地 Mock 冒烟不能替代真实 Vision、Redis、JWT 和生成 Provider 联调。

