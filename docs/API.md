# API 契约

更新日期：2026-08-19（多宠物支持）

## 鉴权

- `MOCK_MODE=true`：必须提供 `X-User-Id`，只用于本地隔离。
- `MOCK_MODE=false`：必须提供 `Authorization: Bearer <JWT>`。
- JWT 必须包含 `sub`、`tenant_id`、`exp`、`iss`、`aud`，并包含 `pet:consult` scope。

## 问诊

### `POST /api/v1/consult`

请求类型：`multipart/form-data`

| 字段 | 必填 | 约束 |
|---|---|---|
| `conversation_id` | 是 | 1-128 字符，只允许字母、数字、下划线和连字符 |
| `text` | 否 | 最多 4000 字符；与图片至少提供一项 |
| `pet_info` | 否 | JSON 字符串（**对象或数组**，见下） |
| `pet_ref` | 否 | 指定本次问诊宠物：宠物 name 或数组下标字符串（多宠时用） |
| `images` | 否 | 最多 3 张，单张最多 5 MB |
| `Idempotency-Key` | 否 | Header，最多 128 字符 |

`pet_info` 兼容两种格式（2026-08-19 起支持多宠物）：

**单只（旧格式，兼容）**：

```json
{
  "species": "cat",
  "breed": "British Shorthair",
  "age_months": 24,
  "weight_kg": 4.5,
  "sex": "female",
  "neutered": true,
  "chronic_conditions": [],
  "current_medications": []
}
```

**多只（新格式，数组）**：

```json
[
  {
    "name": "豆豆",
    "species": "dog",
    "breed": "金毛",
    "sex": "male",
    "age_value": 2,
    "age_unit": "year",
    "weight_kg": 28.5,
    "neutered": true
  },
  {
    "name": "咪咪",
    "species": "cat",
    "breed": "英短",
    "sex": "female",
    "age_value": 8,
    "age_unit": "month",
    "weight_kg": 4.2
  }
]
```

`PetInfo` 字段说明：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 多宠必填 | 宠物名称（区分宠物 + 追问引用） |
| `species` | string | 是 | dog / cat（RAG 物种过滤 + 急症匹配） |
| `breed` | string | 否 | 品种 |
| `sex` | string | 否 | male / female |
| `age_value` + `age_unit` | int + month/year | 否 | 新年龄格式，自动换算 age_months |
| `age_months` | int | 否 | 旧年龄格式（兼容，同时给时以新格式为准） |
| `weight_kg` | float | 否 | 体重 kg |
| `neutered` | bool | 否 | 是否绝育 |
| `chronic_conditions` | array | 否 | 慢性病 |
| `current_medications` | array | 否 | 当前用药 |

多宠选择顺序（2026-08-25 修复）：

1. 显式 `pet_ref`（名称或从 0 开始的数组下标）；
2. 问题正文中唯一出现的宠物名称；
3. 问题正文中的“猫/狗/犬”与列表中唯一匹配的 `species`；
4. 仍无法确定时，为兼容旧调用方回退到数组第一只。

调用方在同物种多宠或问题同时涉及多只宠物时应显式传 `pet_ref`，避免歧义。


主要响应字段：

```json
{
  "request_id": "req_xxx",
  "conversation_id": "conv_001",
  "status": "success",
  "answer_mode": "normal",
  "answer": "...",
  "possible_explanations": [],
  "what_to_do_now": [],
  "avoid_actions": [],
  "what_to_monitor": [],
  "risk_level": "low",
  "risk_flags": [],
  "vet_recommendation": {
    "recommended": false,
    "urgency": "none",
    "reason": ""
  },
  "image_findings": [],
  "follow_up_questions": [],
  "disclaimer": "...",
  "knowledge_degraded": false
}
```

### `POST /api/v1/consult/stream`

请求字段与普通问诊相同，响应为 `text/event-stream`。业务后端必须关闭响应缓冲。

主要事件：`accepted`、阶段进度、可选 `urgent_guidance`、`final`、`error`。

## 会话

- `GET /api/v1/conversations/{conversation_id}`：读取当前用户拥有的会话。
- `DELETE /api/v1/conversations/{conversation_id}`：删除当前用户拥有的会话。

租户和用户身份只从已验证 JWT 获取，不能从请求体覆盖。

## 健康检查

- `GET /health/live`：只表示 API 进程存活。
- `GET /health/ready`：检查 Redis、VisionGateway，以及 `GUARD_MODE=enforce` 时的 Guard。

RAG Shadow 状态放在 `shadow_checks` 中，不参与主服务 ready 判定。

## 错误格式

```json
{
  "request_id": "req_xxx",
  "status": "error",
  "error": {
    "code": "BAD_REQUEST",
    "message": "请求参数格式不正确",
    "retryable": false
  }
}
```

常见状态码：400 参数或图片错误、401 鉴权错误、404 会话不存在、409 会话或幂等冲突、429 限流、503 外部依赖不可用、504 总时限耗尽。
