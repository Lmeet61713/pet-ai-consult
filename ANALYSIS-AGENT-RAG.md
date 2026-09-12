# pet-consult v7.3 —— Agent 与 RAG 子系统解析

分析日期：2026-08-25 交付包 `20260825-image-text-v7.3-rag2-retry1-multipet1`
分析方法：文档通读 + 源码逐文件阅读 + 实际运行项目自带校验/评测/测试脚本
所有结论均标注 文件:行号；带 ✅ 的条目为本报告作者实测复现。

---

## 0. 一句话结论

**这个系统不是 Agent，是"固定流水线 + 确定性降级漏斗"；RAG 不是向量检索系统，是"词法为主、向量为辅的两阶段重排"，且知识资产全部处于未签审的测试态。**

三个最容易被文档误导的地方：

1. `app/agent/state_machine.py`（528 行、14 状态、注释详尽）**在运行时完全没有被引用** —— 它是文档，不是代码。
2. `RAG_MODE` 的"生产强制 off"（`docs/RAG.md:32`）**不存在**，实际生产是 `grounded`。
3. RAG 的评测指标（recall@1=0.99）**是自指种子题**，而唯一有判别力的"拒绝探测"在 v1_8 上只有 **2/4**。✅

---

## 1. 文档地图与新鲜度

| 文档 | 日期 | 可信度 | 说明 |
|---|---|---|---|
| `HANDOFF-v7.3-COMPLETE-20260825.md` | 08-25 | **高** | 最权威、最新，与代码基本一致 |
| `README.md` | 08-25 | 中 | RAG 指向 v1_8 正确；但"绝对 deadline 120 秒"与代码/模板都不符 |
| `docs/ARCHITECTURE.md` | 08-25 | 高 | 链路图准确 |
| `docs/API.md` | 08-19 | 高 | 响应字段与 `ConsultResponse` 一致 |
| `assets/rag/v1_8/validation_report.v1_8.json` | 08-17 | **高（唯一真相）** | RAG 资产状态的唯一权威来源 |
| `assets/rag/v1_8/README.v1_8.md` | — | 中 | 推荐的 rerank 步骤不存在 |
| `docs/RAG.md` | **08-15** | **低** | ❌ 只写 v1_4/v1_5，与已交付的 v1_8 脱节 |
| `docs/FLOWCHART.md` | — | 低 | ❌ 源码行号锚点全部失效；描述的能力默认关闭 |
| `docs/LOCAL_MODEL_PLAN.md` | 08-14 | 低 | ❌ 写"规划中，尚未实现"，实际早已落地 |
| `SCRIPTS_RUNBOOK.md` | — | — | 运维脚本手册 |

---

## 2. Agent 子系统

### 2.1 定位：固定流水线，不是自主 Agent

全仓没有 `tools` / `function_call` / `tool_choice` 定义，没有"模型决定下一步"的循环。`ConsultAgent._execute()`（`app/agent/consult_agent.py:549-1144`，约 600 行）是一条**顺序写死的流水线**，控制流由 Python `if/elif/return` 决定。

模块自己承认了这一点（`state_machine.py:18-20`）：

> 固定状态机：不会自己拆解任务、自由调用工具的通用 Agent。它的"智能"体现在：何时降级、何时追问、何时走固定模板；所有状态转移都是预定义的，没有运行时动态决策。

**准确的说法**：这是一个 *deterministic triage orchestrator*（确定性分诊编排器）。

### 2.2 调用链

```
HTTP /api/v1/consult        → api/consult.py:760
HTTP /api/v1/consult/stream → api/consult.py:816 → _consult_event_stream:603
队列 Worker                  → core/dependencies.py:299 normal_handler → :344
                                        ↓
                     ConsultAgent.run()          consult_agent.py:286
                     ConsultAgent.run_stream()   :304
                     ConsultAgent._run_impl()    :324   ← 幂等/锁/deadline/异常收敛/存档
                     ConsultAgent._execute()     :549   ← 真正的主流水线（13 阶段）
```

队列车道分流在 `api/consult.py:219-340`：车道 1 急症（同步 `agent.run`）、车道 2 RAG 直答、车道 3 登记 + `wait_result()` 轮询。

### 2.3 主流水线 `_execute()` 阶段表

| # | 阶段 | 行号 | 产物 / 分支去向 |
|---|---|---|---|
| 1 | 文字急症预判兜底（纯规则） | `:597-600` | `text_emergency_precheck` |
| 2 | 加载历史（Redis） | `:603-609` | `history` / `history_summary`；失败降级 |
| — | 物种推断 | `:612` | `_apply_inferred_species` |
| 3 | 输入审核（规则 + Guard） | `:626-648` | `parse_ok=False`→急症模板或 REVIEW；`should_refuse`→REFUSE |
| 3.5 | **非宠物问诊短路** | `:652-668` | 天气/气温/下雨三条正则，命中即返回固定文案 ✅ |
| 4 | 图片分析（VisionGateway→4B） | `:671-732` | `vision_findings`；超时/失败→降级 |
| 4.5 | 图文物种冲突 / 无宠物检测 | `:746-748` | 冲突时清空 `vision_findings`（以文字为准） |
| 5 | **RAG 检索**（旁路，异常吞掉） | `:750-789` | `rag_result` / `rag_evidence` / `rag_questions` |
| 5.1 | 完整度判断 + 后置改写 | `:791-826` | `completeness` |
| 6 | 急症规则 + 风险聚合 | `:828-839` | `emergency_result` / `risk_result` |
| 6.4 | EMERGENCY 固定急症模板 | `:840-849` | 先 `_save_turn` 再返回 |
| 6.5 | 卡片直答通道（**默认关闭**） | `:851-868` | `_fast_answer()` |
| 6.6 | 多宠歧义固定追问 | `:870-879` | `_fixed_pet_ambiguous_response()`（**不落历史**） |
| 7 | 生成（三模式 + 1 次重试） | `:881-980` | `generated` |
| 8 | 医疗审核 → 本地修复 → 重写一次 → 固定模板 | `:982-1081` | `medical_review` |
| 8.5 | 无证据收口 + 面向用户语言清理 | `:1083-1116` | |
| 9 | 输出审核 | `:1118-1139` | `blocked` → REVIEW |
| 10 | 组装响应 + 存历史 | `:1141-1144` | |

### 2.4 `state_machine.py` 是死代码（最重要发现）

| 检查 | 结果 |
|---|---|
| 全仓 grep `AgentState` / `_ALLOWED` / `assert_transition` | 命中**仅**在 `state_machine.py` 自身 + 2 处 docstring（`agent/__init__.py:9,14`、`state.py:61`）✅ |
| `tests/` 中引用 | **0 命中** ✅ |
| `consult_agent.py` 是否 import | **否** |

`state_machine.py:414` 声称 "运行时校验：`Agent._execute()` 中每次状态转移前调用 `assert_transition`" —— **与代码不符**。`state_machine.py:62-66` 要求"新增状态时必须补状态机测试" —— **不存在任何此类测试**（`tests/` 仅 7 个文件，共 47 条用例 ✅）。

即使当作文档，`_ALLOWED` 也与真实控制流脱节：
- 未建模的实际分支：out-of-scope 短路 `:652`、vision_failed→REVIEW `:744`、fast answer `:866`、pet_ambiguous `:877`、REFUSE `:647`、生成失败 `:965/978`。
- `COMPLETENESS_AND_RISK → FIXED_SAFE_ANSWER` 标注"急症+预算不足"，但 EMERGENCY 实际走 `_fixed_urgent_response()` `:840-849`。
- `state_machine.py:368` 提到 `state.fixed_safe_answer` —— **`ConsultState` 中不存在该字段**。
- 真正的任务/DB 状态机在 `app/tasks/service.py`（`registered/queued/…/dead_letter`），与本文件无关。

### 2.5 状态模型 `ConsultState`

`app/agent/state.py:83-371`，Pydantic 模型，是各阶段共享的"黑板"。设计干净：`from_command()` 做反向隔离，服务层只依赖 state。

已确认的问题：

- **`state.status` 从未被赋值** ✅（`state.py:305` 声明，docstring 说"由 `_execute()` 决定"，实际无任何写入点；状态只在 `ConsultResponse.status`）。
- **`history_summary` 永远为空** ✅：`:606` 读 Redis `summary_key`，但唯一写入口 `ConversationService.save_summary`（`conversation_service.py:54`）/`ConversationRepository.save_summary`（`conversation_repository.py:80`）**全仓无调用者**。即"长历史压缩"未实现，`conversation_summary` 恒为 `""`。
- **3 个动态属性未声明**：`state._steps`（`:594`）、`state._species_source`、`state._generate_retried` —— Pydantic v2 对下划线名走 `_object_setattr`，能跑但不参与校验/序列化。

### 2.6 生成模式与 `answer_mode` 的三层不一致

模式选择逻辑被抄了三遍：

| 位置 | 条件 |
|---|---|
| `:889-907`（非流式主路径） | HIGH→urgent；`reason ∈ {hard_need, keyword_thin}`→provisional；否则 normal |
| `:933-951`（重试路径） | 同上 |
| `:1185-1197`（流式） | 同上，但多了一个 `pet_ambiguous`（**该 reason 在 `:872` 已被拦截 → 死分支**） |

更关键的是：`mode` 这个局部变量**只用于日志和 steps**（`:893/902/907/909`），**从不写回 `state.generated.answer_mode`**；最终响应直接取模型自报值（`_answer():1524` `answer_mode=g.answer_mode`）。医疗审核也不校验 `answer_mode`（`medical_checker.py:78-104` 只查 risk_level / vet_urgency）。**即模型可以把 normal 请求答成 provisional，无人纠正。**

### 2.7 追问与多轮

**`FollowUpTracker`**（`followup_tracker.py`）—— 确定性词表/正则槽位抽取，**第一期只覆盖眼部**（`domain="eye"`，`extract():151-320`）。非 eye 域直接返回空 `CaseFacts`，不做任何抽取。

- 5 个槽位按序：`duration → discharge_character → eye_discomfort → general_condition → eye_closeup`（`:280-288`）
- 否定语境：`_NEGATION_RE`（`:109`）匹配信号词前 10 字符内的否定词
- 追问映射 `_SLOT_QUESTIONS`（`:127-133`），`questions(limit=3)`

**`CompletenessChecker.evaluate()`**（`completeness_checker.py:145-252`）—— 9 条判据，命中即返回：

| 判据 | 条件 | reason |
|---|---|---|
| 1 | 多宠且无法确定问哪只 | `pet_ambiguous` |
| 2 | 无图 + 无文字 + 无历史 | `hard_need` |
| 3 | 存在 `image_quality == "unusable"` | `hard_need`（请重拍） |
| 4 | `case_facts.domain == "eye"` 且有缺失槽位 | `hard_need`（结构化眼部追问） |
| 5 | 任一图 `needs_more_images` | `hard_need`（补角度） |
| 6 | 有图但全程无文字 | `hard_need` |
| 7 | 日常护理频率问题 | `general_care`（信息充足） |
| 8 | `_information_too_thin` | `keyword_thin` |
| 9 | 其余 | 信息充足 |

判据 8 的阈值（`_information_too_thin:254-268`）：**7 类信息词表覆盖 < 2 类 且 拼接文本 < 80 字**。

`_thin_questions`（`:270-357`）按症状分支（歧义排泄 / 腹泻 / 猫黑下巴 / 呼吸道 / 精神沉郁 / 兜底），**最多 2 问**，且过滤 `case_facts.asked_questions` 去重。

> **副作用耦合**：`case_facts` 由 `CompletenessChecker.evaluate()` 内部调用 `FollowUpTracker.extract()` 写入（`completeness_checker.py:176`），而 `RiskEngine` 读它（`risk_engine.py:113`）。调用顺序在 `_execute` 中正确（`:792` 早于 `:836`），所以眼部升档是活的 —— 但这是一个隐藏的时序依赖。

### 2.8 风险聚合 `RiskEngine`

`risk_engine.py:67-160`，**"只升不降"的 max-merge**，三路信号：

1. **主信号**：`emergency_result`（`EmergencyRuleEngine.evaluate`，`safety/emergency_rules.py:129-174`）。无命中→LOW（脆弱宠物 MEDIUM）；有命中→取最高，**脆弱宠物各升一级**；图片 red_flag 关键词兜底（`:165-172`）。
2. **图片质量升档**（`:101-110`）：仅 LOW + 存在 `poor` → MEDIUM + BOOK_VET。
3. **眼部专科升档**（`:112-153`，需 `domain=="eye"`）：
   - 脓性/黄绿/黏稠分泌物 → MEDIUM + BOOK_VET
   - 眯眼/抓挠/畏光/疼痛/睁不开 → MEDIUM + BOOK_VET
   - 眼球突出/突然失明/眼部出血/眼部外伤 → **HIGH + URGENT**
   - `duration_hours > 48` → MEDIUM + BOOK_VET
   - 脓性 + 不适同时出现 → urgency 升到 WITHIN_24_HOURS（**只升 urgency 不升 level**）

**非眼科的专科升档不存在** —— 这是当前风险引擎的能力边界。

`_answer():1519` 最终风险取 `max_level(risk_result.level, 模型自报 risk_level)`。

**`_risk_flags()`（`:1915-1935`）语义混装**：把临床原因码（`eye_fact:purulent_discharge`）与基础设施降级标记（`redis`/`vision`/`knowledge_consult`）放进同一个数组，前端无法区分。

### 2.9 安全兜底漏斗（23 条路径）

| 兜底 | 触发条件 | 位置 | 结果 |
|---|---|---|---|
| Redis 降级 | `RedisUnavailable` | `:412,455,607` | 无锁无记忆继续 |
| Vision 降级 | Timeout/Unavailable/OutputInvalid | `:690-696` | 清空 findings，转纯文字 |
| Vision 子预算耗尽 | 子 deadline 耗尽但总预算剩余 ≥0.1s | `:697-707` | 放弃图片继续 |
| 纯图无文字 + Vision 失败 | `vision_failed and not has_text_context` | `:742-744` | REVIEW(`image_unavailable`) |
| 物种冲突 | `_detect_species_conflict()` | `:804-816` | 清空图片观察，追问置顶 |
| RAG 失败 | 任意异常 | `:787-788` | 仅 log，主链路不受影响 |
| 生成失败 | 超时类或预算不足 | `:966-978` | **ERROR**（见下） |
| 流式增量违规 | `StreamSafetyAbort` | `:910-918` | 固定安全模板 |
| 医疗审核不过 | → `repair_locally` → 复审 | `:992-1019` | 只改违规字段 |
| 仍不过 | → `rewrite_once`（携带违规清单） | `:1020-1051` | 重写一次 |
| 仍不过 | → `build_fixed_safe_answer` | `:1063-1064` | 固定安全模板 |
| 输出审核拦截 | `output_moderation.blocked` | `:1132-1133` | REVIEW |

**漏斗设计的可取之处**：本地确定性修复优先于模型重写（省时且不丢已正确的针对性内容），重写仅一次，最终必然收敛到固定模板。"回答优先"原则落地为：高风险不短路（切 `urgent_guidance`）、信息不足不终止（provisional）。

> **⚠ 漏斗漏洞**：生成失败的两条路径（`:958-978`）都直接 `return self._service_unavailable(...)` → **ERROR**，既不判断 `risk_result.level == HIGH`，也不调用 `build_fixed_safe_answer`。而 `:884-893` 已明确选了 `urgent_guidance` 模式。**高风险病例在 9B 不可用时可能拿到 `status=error`**，永远走不到 D15/D16。

### 2.10 超时纪律

- 绝对 deadline（`core/deadline.py`），`child(cap)` 同时受父级约束，**重试不重置预算**。
- 阶段预算：Vision 15s（`:684`）、生成 20s（`:905`）、重试 15s（`:935-949`）、安全重写 15s（`:1020`）、Guard 各 1.5s。
- **超时不重试**（防重试风暴），实现方式（`consult_agent.py:98-118`）：

```python
def _should_retry_knowledge_failure(exc: KnowledgeConsultUnavailable) -> bool:
    return "超时" not in str(exc)
```

  配合 `:922-926` 的 `and deadline.has_remaining(12.0)`。

  > **⚠ 脆弱耦合**：这是对**异常消息里中文字符串**的子串匹配，依赖 `knowledge_consult_client.py:114/177/384/464` 恰好抛出 `"知识问诊超时"`。任何一处理措辞改动都会静默失效掉防雪崩保护。且只覆盖 httpx 超时，deadline 中止是另一类异常。

- **不可配的手写常量**：`12.0` / `15.0` / `20.0` / `0.1` 散落在代码里，未从 `Settings` 派生。
- `app/utils/timeout.py::run_with_timeout()` **全仓无调用** → `except ExternalServiceTimeout`（`:505`）实际不可达。

### 2.11 Agent 侧其他已确认缺陷

1. **⚠ `ShadowRetriever.build_fast_answer` 签名不兼容 → 必然 `TypeError`** ✅
   - 调用方 `consult_agent.py:1271-1276` 传 `query_species=`
   - `rag/retriever.py:285` 定义是 `def build_fast_answer(self, result: RagResult) -> dict:`（**无该参数**）
   - 只有 `hybrid_retriever.py:461` 接受 `query_species`
   - 当地 `RAG_EMBEDDING_MODEL_PATH` 为空（选 `ShadowRetriever`）+ `RAG_FAST_ANSWER=true` 时，直答路径必抛 `TypeError`，且不在任何 `try` 内 → 冒泡为 `INTERNAL_ERROR`。
   - 当前 `rag_fast_answer` 默认 `False`（`config.py:108`）掩盖了它。

2. **⚠ 多宠歧义响应不落历史**（`:872-879`）：对比急症分支 `:848` 的 `await self._save_turn(...)`。后果：该轮不进 Redis 历史、`follow_up_questions` 不进 `asked_questions`、下一轮 `history` 看不到"系统问过是哪只" —— 而这条路径**恰恰就是要问用户"是哪一只"**。

3. **⚠ B1 急症短路不落历史、不存档**（`:390-399`，无幂等键时直接 return），跳过 `:541-542` 的 `_archive_dialogue()` 与 `_save_turn()`。同一文件内的另一条急症路径（`:840-849`）两者都有 → 倾向于是疏漏。后果：观测数据缺失全部"无幂等键的急症请求"。

4. **⚠ Worker 关停泄漏** ✅：`dependencies.py:427/439` 把 worker 任务收进 `self._worker_tasks`（list），但 `shutdown():563-566` 检查的是 `self._worker_task` —— 该变量只在 `:505` 被设为 `None`，**从未被赋值为任务**。对比 `_outbox_task:469`、`_monitor_task:491`、`_dialogue_cleanup_task:423` 三个循环都正确赋值并被取消 → 这是孤立疏漏，不是模式。后果：WorkerLoop 与消费协程在关停时永不取消。

5. **`config.py:61 mock_vision: bool = True`（默认）** 会让 `_detect_species_conflict()` 直接返回 False（`:1323`）→ 默认配置下图文物种冲突检测**完全关闭**。生产 compose 设 `MOCK_VISION=false`（`compose.yaml:228`），所以生产是开启的 —— 但本地/默认配置下的行为与文档不符。

6. **`_is_out_of_scope_query` docstring 与实现不符**：docstring（`:1410-1412`）说识别"股票查询、人类医疗问题、闲聊"，实际只有天气/气温/下雨三条正则（`:127-131`）。

7. **Prompt 版本三处不一致**：`consult_agent.py:1987` 硬编码 `"consult_answer_first_v2.4.0"`；`consultation_answer_first_v2.py:11` 是 `PROMPT_VERSION = "v2.5.0"`；同文件 docstring 写 `v2.1.0`；`RELEASE_V7_2.md:17` 又说 v2.4.0。

8. **同一 SYSTEM prompt 配两套互斥输出契约**：`consultation_answer_first_v2.py:69-84` 要求两段式 `<answer>…</answer><json>…</json>`，而 `LocalOpenAIAdapter` 强制 `response_format=json_schema`（`:334-337`）→ 模型不可能产出 `<answer>` 段。非流式 local 路径靠 `_answer:1525` 的 `g.answer_text or self._render(g)` 兜底。

9. **4 个 prompt 模块的 `SPEC` 从未被导入**：`provisional_v1` / `urgent_guidance_v1` / `rewrite_v1` / `medical_review_v1`。三种模式 + 重写 + 流式**全部共用同一个 system prompt**。`PromptRegistry` 只被 `vision_gateway_client` 使用 —— "版本化注册表"的设计在咨询侧未落地。

10. **其他死代码**：`self.input_moderator`/`self.output_moderator`（`consult_agent.py:267-268`，赋值后 0 次使用）；`utils/retry.py::retry_async`（无调用者）；`EmergencyRuleEngine.hints()`（`:226-230`）；`evaluate(vision_findings=…)` 参数声明后从未使用（`:135`）；`ImageService.build_summary()`（`image_service.py:121-133`）被 `knowledge_consult_service.py:38-42` 内联重复。

11. **`app/agent/consult_agent.py.original`（74,647 B）随包分发** ✅ —— 源码目录中的备份文件，非 `.py` 不会被导入，但污染交付包。

12. **`knowledge_max_tokens` 默认 0 = 应用侧不限制生成长度**（`config.py:95`），compose 也未设置 → 生成长度只受 deadline 约束。

---

## 3. RAG 子系统

### 3.1 资产版本谱系（✅ 实测计数）

| 版本 | 知识卡 | 急症规则 | 来源 | 基准种子 | 审核队列 | 类别 | `production_eligible=true` |
|---|---:|---:|---:|---:|---:|---:|---:|
| v1_4 | 260 | 40 | 116 | 780 | 300 | 27 | 0 |
| v1_5 | 260 | 40 | 116 | 778 | 300 | 27 | 0 |
| v1_6 | 260 | 40 | 116 | 778 | 300 | 27 | 0 |
| v1_7 | 304 | 40 | 116 | 912 | 344 | 27 | 0 |
| **v1_8（运行时默认）** | **319** | **40** | **131** | **957** | **359** | **27** | **0** |

- `docs/RAG.md` 描述的是 **v1_4/v1_5**（260 卡 / 116 来源）—— 已过期两代。
- 运行时默认路径硬编码为 v1_8（`dependencies.py:181-185`，`RAG_INDEX_PATH` 为空时 `assets/rag/v1_8`）✅
- 跑项目自带校验 ✅：`shadow_ready=true, production_ready=false, errors=[], pending_card_reviews=319`

### 3.2 加载与校验（`loader.py`）—— 本子系统最扎实的部分

`RagAssetLoader.load()`（`:86-146`）执行多层静态闸门，任一失败累积错误码，`report.ready=False` 时检索器直接返回 `UNAVAILABLE`，**绝不带病提供知识**：

1. `SHA256SUMS` 整目录校验（`:355-381`）
2. 逐条 `content_hash` 重算比对（`:263-267`）
3. **审核队列哈希一致**（`:268-269`）—— 证明"送审版本 == 加载版本"，逐字节相同
4. 发布闸门：`index_tier == "test"` 且 `production_eligible is False`（`:405-418`）
5. `veterinary_review.status == "pending"`（`:260-262`）
6. 证据可溯源：`source_id` 必须存在于来源清单，且必须带 `page_or_section` 定位（`:270-275`）

这套"未签审即无法加载/无法发布"的设计思路是**正确的**，也是这份交付里最值得保留的部分。

**⚠ 但有一个闸门缺口**（`:317-322`）：

```python
if asset_format in ("v1_4", "v1_5", "v1_6", "v1_7") and (
    source.get("commercial_use_allowed") is not True
    or "NC" in str(source.get("license", "")).upper()
    or "ND" in str(source.get("license", "")).upper()
):
    errors.append(f"disallowed_source:{source_id}")
```

**v1_8 被排除在外** —— 而 v1_8 正是运行时默认版本。实测 v1_8 的 131 条来源 ✅ 全部 `commercial_use_allowed=true` 且无 NC/ND，**当前没有实际违规**；但该闸门已不再保护默认资产版本，未来 v1_9 同样不受保护。这正是 v1_8 校验报告自己承认的 "仍有部分 V1.7 遗留来源缺少结构化许可核验状态"。

### 3.3 检索算法（`retriever.py`）—— 纯词法，无向量

**评分公式**（`retriever.py:194-201`）：

```python
final_score = min(1.0,
      keyword_score * 0.72
    + title_score   * 0.12
    + phrase_score  * 0.20
    + species_score            # 0.08，有物种时
    + category_score)          # 0.04，显式分类命中时
```

- **词法化**（`_terms:290-300`）：CJK 整段 + 全部 **2-gram** + ASCII 单词，统一小写。
- **重叠度**（`_overlap:303-306`）：`|Q∩C| / |Q|`（按查询侧归一，非对称 —— 长卡片不吃亏）。
- **候选门禁**（`:192-193`）：`keyword<=0 and phrase<=0 and category<=0` → 跳过。**物种加分本身不能让卡片入候选**（防跨病种污染）。
- **查询停用词**（`_QUERY_STOPWORDS:310-317`）：过滤"怎么办/什么原因/猫咪/狗狗"等，避免稀释症状信号。
- **未知物种**（`:181-183`）：只允许 `species` 同时含 cat 和 dog 的通用卡，禁止随机落到单一物种。
- **阈值** 0.24，`top_k` 4。

**前置短路门禁**：
- `is_vague_general_query`（`:337-344`）—— "没精神/状态不好"等非特异性描述且无具体症状 → INSUFFICIENT
- `is_ambiguous_elimination_query`（`:347-353`）—— "上厕所"未区分排尿/排便 → INSUFFICIENT
- `is_cat_chin_specific_query`（`:356-366`）—— 猫下巴皮损 → INSUFFICIENT
- `_POLICY_TERMS`（`:32`）—— 剂量/mg/停药/换药/处方 → **POLICY_RESTRICTED**（禁用卡片答用药）

> **⚠ 硬编码卡片 ID**：`preferred_card_id()`（`:369-382`）把 `V17-CAT-PED-001`（幼猫驱虫/疫苗）和 `MVP-UR-001`（犬排尿）写死在检索逻辑里，由 `promote_preferred_card`（`:385-394`）强制提到 rank-1。资产重命名会静默破坏该偏好。

### 3.4 混合检索（`hybrid_retriever.py`）—— BGE-M3 是真的，但没有 reranker

**是真的向量检索**（`EmbeddingClient:47-135`）：
- `FlagEmbedding.BGEM3FlagModel`，dense 1024 维，L2 归一化，`float32`
- fp16 **仅在 CUDA 可用时**启用（`:85-87`，CPU 上 fp16 会卡死，2026-08-18 修复）
- **后台线程加载**，不阻塞启动；索引就绪前自动回退纯词面（`:231-254`）
- 卡片向量持久化到 `runtime/bge_card_emb.npy`，重启免重算（`:210-228`）
- 查询带 BGE 检索指令前缀 `"为这个句子生成表示以用于检索相关文章："`（`:42`）

**两阶段架构**（`search:256-427`）：
1. 词面粗筛全量卡片 → 取 `coarse_top_k=30`（避免无关卡片参与混合分）
2. 精排：`final = alpha * lexical + (1-alpha) * dense`，`alpha=0.7`
3. 阈值 0.32（纯词面回退时 0.24）；无物种信息时等额放宽 `0.08*alpha`（`:407-409`）

**⚠ 已确认的缺口**：

1. **全仓没有任何 reranker** ✅ —— grep `rerank|CrossEncoder|BM25|faiss|pgvector` **零命中**。而 `README.v1_8.md:18` 推荐的流程是"急症规则 → 物种过滤 → 关键词/向量混合召回 → **rerank** → 生成回答 → 红旗复核"。**rerank 步骤不存在**，实际是 alpha 加权融合。也不是 RRF —— 是直接加权和。
2. **向量无法召回词面漏掉的卡** ✅ —— dense 只作用于词面粗筛出的 ≤30 张候选（`:372-378`）。这是"混合召回"的实质上限：词面没粗筛到的卡片，向量永远救不回来。
3. **无 ANN 索引** ✅ —— 没有 FAISS / pgvector，全量卡片向量在进程内做 `np.asarray(card_emb) @ np.asarray(q_vec)[0]`（`:333`），O(N)，N=319。当前规模无妨，但不可扩展。
4. **`semantic_score` 恒为 0.0** ✅ —— `retriever.py:219` 与 `hybrid_retriever.py:399` 都硬编码，即使 dense 分参与了 `final_score`。**每跳的语义分在日志/指标中永不可观测。**
5. **`embedding_model_version` 恒为占位值** ✅ —— `models.py:72` 默认 `"none-lexical-shadow"`，**全仓无任何赋值点**。所以即使混合检索正常工作，上层也无法区分"混合"与"纯词法"。
   > 与 4 合起来：**BGE-M3 是否真的生效，从日志和响应里完全无法验证。**
6. **⚠ 生产上 BGE-M3 跑在 CPU 上** ✅ —— `Dockerfile.api:29` 安装的是 `torch-2.13.0+cpu`（Dockerfile 第 3 行注释也写明 "BGE-M3(FlagEmbedding + CPU torch)"），而 `hybrid_retriever.py:85-86` 是 `use_fp16 = torch.cuda.is_available()` / `device = "cuda" if use_fp16 else "cpu"`。`consult-api-1` 在 compose 里**没有 GPU 预留**（`compose.yaml:216-274`）→ 容器内 `cuda.is_available()==False` → **CPU float32**。
   > ⚠ 而 `hybrid_retriever.py:3` 的 A/B 结论标注是 "2026-08-15, AutoDL RTX 5090"（GPU fp16）。**调参环境与生产环境的 dense 分分布不同**，0.32 阈值是在 GPU fp16 下选出的。
7. **`warmup()` 无条件返回 `False`** ✅ —— `hybrid_retriever.py:254` 是死 `return False`，所以 `dependencies.py:205-212` 的 `if self.rag_retriever.warmup():` 永远走 else 分支，日志永远打印"后台预热中(模型就绪后自动启用)"，`logger.info("混合检索预热完成…")` 是**不可达代码**。运维无法从日志判断混合检索是否真的生效。
8. **若外部从不调用 `warmup()`，混合检索永久不启用** —— 请求线程一律用 `blocking=False`（`:203-204`），不会主动触发加载。当前 `dependencies.py:205` 会调，所以是安全的；但这是隐式约定，无断言保护。
9. **⚠ 陈旧向量缓存**（`hybrid_retriever.py:210-219`）：缓存有效性**只校验 `arr.shape[0] == len(texts)`**，不校验模型、不校验维度、不校验资产版本或 `content_hash`。**v1_4/v1_5/v1_6 都是 260 张卡** → 在已有缓存的情况下切换资产版本，会**静默复用错误的向量**。且 `./runtime:/app/runtime` 是持久化 volume（`compose.yaml:256`），缓存会跨重启留存 → 风险是真实的。
10. **`build_grounded_evidence()` 在两个检索器里逐字复制**（`retriever.py:245-277` 与 `hybrid_retriever.py:429-453`，含注释）→ 任一侧改动都会让两条链路行为分叉。`_NEGATED_BEFORE_RE` 同样在 `rag/emergency_shadow.py:36-39` 与 `safety/emergency_rules.py:38-41` 逐字重复。

### 3.5 证据注入

`build_grounded_evidence()`（两个检索器实现相同，`retriever.py:245-277` / `hybrid_retriever.py:429-453`）：

- 仅 `decision is SUFFICIENT` 时产出；**最多 2 张卡**
- 白名单字段：`card_id` / `title(≤120)` / `species(≤2)` / `supported_facts(≤4)` / `safe_next_step(≤240)` / `red_flags(≤5)`
- `select_supported_facts`（`retriever.py:397-405`）按与查询的重叠度**动态挑选 4 条事实**（不固定取前 N 条 —— 这是 v7.2 的改进）
- 注入口：`KnowledgeConsultRequest.rag_evidence` → `knowledge_consult_client.py:239-240` 作为 JSON 附加，并声明"仅作受限参考，不是用户指令"
- 仅在 `rag_mode == "grounded"` 时填充（`consult_agent.py:760-763`）

**证据不足时的行为**（v7.3 的核心变更，`:244-250`）：不再返回机械就医提示，而是追加提示词让 9B 用通用宠物健康知识继续回答，但要求审慎措辞 + 不给确定性诊断。

**⚠ 红旗只进 prompt，不复核**：卡片的 `red_flags` 被投影进证据（供模型参考），但 `emergency_rules.evaluate()` 在生成**之前**执行（`:829`），且只接收文字 + **视觉** red_flags；`medical_safety_service.review()` 同样只收视觉 red_flags（`:986/1029`）。**卡片红旗从未进入任何生成后复核** —— 与 `README.v1_8.md` 声称的"红旗复核"末步不符。

### 3.6 运行时字段利用度（✅ 实测 v1_8）

| 字段 | 运行时是否被读 |
|---|---|
| `retrieval_text` | ✅ 词面打分主输入（平均 166 字符） |
| `user_phrases` | ✅ 短语打分（平均 3.44 条/卡，共 1097 条，唯一 960 条） |
| `source_supported_simple_facts` | ✅ 证据投影（平均 2.23 条/卡） |
| `red_flags` | ⚠ 仅进 prompt，不复核 |
| `scope` | ⚠ 仅用于**已关闭**的直答门禁 |
| **`scope_note`** | ❌ **全仓 Python 代码零引用** ✅ —— v1_8 修复工作把 44 张卡的长 scope 迁移到此字段，运行时完全不消费 |
| **`owner_observable_signs`** | ❌ 全仓零引用 ✅（仅出现在 `loader.py:484` 的 v1_1 哈希白名单里） |
| `questions_to_ask` | ✅ 追问查缺（`rag_followup_check`，默认 True） |
| `safe_next_step` | ✅ 证据投影 |
| `category` | ⚠ 仅当调用方显式传 `category=` 时加分；`_execute:756-759` **不传** → `category_score` 恒为 0 |

### 3.7 三种模式与"生产强制 off"的真相

实现只是两个布尔属性（`config.py:254-260`）：

```python
@property
def rag_shadow(self) -> bool:   return self.rag_mode in {"shadow", "grounded"}
@property
def rag_grounded(self) -> bool: return self.rag_mode == "grounded"
```

- `off` → `rag_shadow=False` → **完全不加载资产**（`dependencies.py:179`），检索器为 `None`；急症影子匹配器也一并关闭（`:235` 嵌在 `:179` 内）
- `shadow` → 加载 + 检索；**不注入卡片原文**
- `grounded` → 相对 `shadow` **只多一行**证据投影（`consult_agent.py:760-763`）

**⚠ `docs/RAG.md:29` 说 shadow 模式"检索和急症匹配只写日志，不影响回答和分诊" —— 不准确** ✅

`rag_grounded` 只守着证据投影那一行。以下三处**不受模式约束**，在 `shadow` 下照样生效：

1. **追问注入**：`if rag_followup_check and decision is SUFFICIENT and hits:` → 取首卡 `questions_to_ask[:3]` 交给 `CompletenessChecker`（`consult_agent.py:764-772` → `completeness_checker.py:247,334`）→ **直接改变问诊的追问内容**
2. **Prompt 分支**：`rag_decision` 无论什么模式都进入请求体（`knowledge_consult_service.py:56`），在 `knowledge_consult_client.py:244-256` 只要 decision 不是 sufficient/空，就追加一整段"用通用知识回答"的指令
3. **确定性收口**：`_apply_provisional_no_evidence_guard()`（`consult_agent.py:1086` → `:1347-1398`）读 `rag_result.decision` / `reason_codes`，命中时清空 `possible_explanations`、重写 `summary`、清空 `answer_text` → **直接改写最终回答**

所以 shadow 的真实语义是"**不把卡片原文喂给模型**"，而不是"只写日志"。`docs/DEPLOYMENT.md:28` 把 `RAG_MODE=off` 列在"生产启动前必须确认"的清单里 —— 那是**部署规范里的建议，不是运行时不变式**。

**❌ `docs/RAG.md:32` 的"生产启动当前强制要求 `RAG_MODE=off`"是错的** ✅ —— 全仓无任何生产校验强制这一点。实际取值：

| 位置 | 值 |
|---|---|
| `config.py:101`（代码默认） | `shadow` |
| `.env.example:49` | `shadow` |
| `.env.docker.example:67`（生产模板） | `grounded` |
| `compose.yaml:247` | `${RAG_MODE:-grounded}` |

即**生产实际是 `grounded`** —— 与 README:14 一致，与 `docs/RAG.md` 矛盾。文档在此处不仅过期，而且方向相反（一个说强制关闭，一个说已在用）。

### 3.8 急症规则影子匹配（`emergency_shadow.py`）

**确认是纯影子**：`V14EmergencyShadowMatcher.search()` 在 `consult_agent.py:364-380` 被调用，结果**只写日志**（事件 `rag_emergency_shadow_result`），不进任何决策。线上分诊由 `app/safety/emergency_rules.py` 负责，两者互不干扰。

与 `loader.py` 同一套闸门（`:133`：`index_tier=test` + `production_eligible=False`；`:16` 审核队列哈希一致）。

自带评测 ✅：40 条规则、121 条触发短语、触发召回 **1.0**、否定误报 **0**、物种误报 **0**。
> 注意：这是**用资产自身的触发短语**做探针（自指），脚本自己在 `limitation` 里承认"改写表达召回与真实误报率仍未测量"。

**与线上急症引擎的关系：两条完全平行的链路，零共享**

| | 影子（RAG） | 线上分诊（safety） |
|---|---|---|
| 数据源 | `assets/rag/v1_*/emergency_rules.combined_*.json` | `configs/emergency_rules.yaml` |
| 规则数 | **40** | **8** |
| ID 形如 | `OS-ER-UR-001` / `MVP-ER-TOX-003` | `breathing_emergency` / `seizure_coma` |
| 结构 | `triggers[]` + `severity{emergency_now,urgent_same_day}` + `action` + `model_constraints[]` | `any_keywords[]` + `context_patterns[]` 正则 + `level: RiskLevel` + `vet_urgency` |
| ID 交集 | **空**（实测 v1_4 与 v1_8 均为空） | |

**⚠ 但两处代码是逐字复制的**：`_NEGATED_BEFORE_RE` 在 `rag/emergency_shadow.py:36-39` 与 `safety/emergency_rules.py:38-41` 完全相同，"信号词前 14 字符窗口"的判定逻辑也是两份实现。

**⚠ 否定词表缺一个裸"不"** ✅ —— 正则交替项是 `没有|没|无|未|否认|并无|不存在|不是|不再|未见|未出现|没有出现|没出现`，**"不"本身不在表内** → `"不呕吐"` 会误触发带"呕吐"的规则。而这个缺陷**在线上分诊引擎里同样存在**（同一份正则）。
> 更糟的是评测测不出来：`evaluate_rag_shadow.py:88-91` 只用 `f"目前没有{trigger}"` 这一个模板测否定 → **121 条否定探针全部测不到裸"不"的情况**，所以"否定误报 0"这个绿灯是假的。

**⚠ 影子匹配器加载即弃的字段**：`action` / `model_constraints` / `evidence` 加载后完全不被使用，`search()` 只返回 `rule["id"]` 与 `severity`（`emergency_shadow.py:172-178`）。
> 其中 **`model_constraints`（"不得输出药物和剂量""不得要求继续观察以替代就医"等硬约束）从未进入任何 prompt** —— 这是安全上有明确价值的信息被浪费。

**⚠ 文档与代码不符**：`emergency_shadow.py:154-155` 的 docstring 说"未知物种时只匹配物种字段兼容该情况的规则"，实际代码是 `if normalized_species and normalized_species not in allowed_species`（`:166`）→ **物种未知时完全不过滤，40 条规则全部参与匹配**。

### 3.9 评测脚本与指标可信度（关键）

`scripts/evaluate_rag_shadow.py` 在 v1_8 上的**实测输出** ✅：

| 指标 | 值 | 可信度 |
|---|---|---|
| 种子题数 | 957 | — |
| recall@1 | **0.9896** | ❌ 自指，无参考价值 |
| recall@4 | **0.9927** | ❌ 自指 |
| MRR@4 | 0.9911 | ❌ 自指 |
| `species_mismatches` | **808** | ⚠ 指标本身有 bug（见下） |
| `rejection_probes` | **2/4** | ✅ 唯一有判别力，结果不佳 |
| 急症触发召回 | 1.0 | ❌ 自指 |

**种子题自指**：脚本自己声明（`:108-111`）"Seed queries are copied from card titles/user_phrases; these metrics are regression checks, not real-user performance"。0.99 的 recall@1 意味着"用卡片标题去检索这张卡片能中"—— 这是回归护栏，不是能力度量。

**⚠ `species_mismatches=808` 是评测脚本的 bug** ✅，我定位了根因：

- `evaluate_rag_shadow.py:48` 写的是 `row["species"].split("|")[0]`
- 基准 CSV 的分隔符在 **v1_8 被从 `|` 改成了 `/`** —— 实测：v1_4~v1_7 是 `cat|dog`（228 行），**v1_8 是 `cat/dog`（228 行）**
- 于是这 228 行传入 `"cat/dog"`，`normalize_species()`（`retriever.py:482-497`）无法识别 → 返回 `None` → 检索器走"未知物种"分支（只允许猫狗通用卡，不加物种分）→ 且校验时 `"cat/dog" not in {card species}` 恒真 → 每跳都计一次错配
- 228 行 × ≤4 跳 ≈ 912 上限，与实测 808 吻合

**双重后果**：(a) 这 228 条多物种查询的召回数是在**错误的候选池**下算出来的；(b) 错配指标失去意义。

**⚠ `rejection_probes` 2/4 是 v1_8 的资产回退，不是配置差异** ✅

我逐版本跑了同一个脚本、同一台机器、同一套纯词法配置（阈值 0.24），只有资产版本不同：

| 版本 | 查询数 | R@1 | R@4 | species_mm | **拒绝探针** |
|---|---:|---:|---:|---:|---:|
| v1_4 | 780 | 0.9910 | 0.9936 | 0 | **4/4** |
| v1_5 | 778 | 0.9910 | 0.9936 | 0 | **4/4** |
| v1_6 | 778 | 0.9910 | 0.9936 | 0 | **4/4** |
| v1_7 | 912 | 0.9550 | 0.9726 | 0 | **4/4** |
| **v1_8** | 957 | 0.9896 | 0.9927 | **808** | **2/4** |

**这是同构对比**：v1_4~v1_7 全部 4/4，唯独 v1_8 掉到 2/4。所以回退**由 v1_8 的资产重建引入**，与检索器配置无关。

失败的两条：`"我家猫怎么了"` → sufficient 0.512；`"狗正常吃饭喝水"` → sufficient 0.286（阈值 0.24）。

根因（逐项复算 `_query_terms`/`_terms`/`_overlap`）：
- `我家猫怎么了` → query_terms = `{么了, 家猫, 我家, 我家猫怎么了, 猫怎}`（5 个），命中 `V17-CAT-GI-001` 的 `retrieval_text` 里的 `么了/家猫/我家` → `3/5 = 0.6` → `0.6*0.72 + 0.08(物种) = 0.512`
- `狗正常吃饭喝水` → 7 个 query term，命中 `V17-DOG-END-002`（犬糖尿病）里的 `喝水/正常` → `2/7 = 0.2857` → `0.2857*0.72 + 0.08 = 0.2857`
- **v1.7/v1.8 把 `user_phrases` 原文拼进了 `retrieval_text`**，于是"我家/家猫/么了/喝水/正常"这类**通用 bigram 变成了可命中的检索信号**，而 `_QUERY_STOPWORDS`（`retriever.py:310-317`）里没有这些词。

**这直接推翻 `hybrid_retriever.py:3-6` 的 A/B 前提**：该注释声称参数是在"拒绝探测 4/4 全过"约束下选出的最优解，而该约束**在当前默认资产 v1_8 上已不成立**。同时，v1_8 的 `retrieval_text` 重建（本意是"清理高频检索短语污染"）反而引入了新的污染 —— 这是本次交付里最值得回滚/重做的一项资产变更。

### 3.10 校验/评测脚本自身的缺陷

| # | 问题 | 证据 |
|---|---|---|
| 1 | **`validate_rag_assets.py:30` 对 v1_8 吞掉急症规则错误** —— 只有 `asset_format in ("v1_4","v1_5","v1_6","v1_7")` 时才把急症规则错误并入 `errors`。**v1_8（运行时默认资产）的急症规则校验错误会被静默丢弃**，既不影响 `shadow_ready` 也无任何提示。（当前 v1_8 规则无错，故未暴露） | `validate_rag_assets.py:30` |
| 2 | **`production_ready` 对 v1_4+ 逻辑上不可达** —— 要求 `total_production_eligible == total_records`，即必须把卡片的 `production_eligible` 改成 `True`；但这必然触发 `loader._validate_closed_release_gate()` 的 `production_eligible_record` 错误 → `errors` 非空 → `production_ready` 恒 False。**两个闸门互相排斥**，切换生产必须改代码，脚本对此零提示 | `loader.py:417-418` vs `validate_rag_assets.py:56-60` |
| 3 | **`shadow_ready` 只看卡片 loader，不含急症规则** —— `RagAssetReport.ready` 不知道急症规则的存在 | `validate_rag_assets.py:55` |
| 4 | **`evaluate_rag_shadow.py` 会"测出失败但返回成功"** —— `:99` 把 `shadow_ready` **硬编码为 `True`**，`main()` 只按它给退出码（`:139`）。所以 v1_8 拒绝探针 2/4 失败，脚本照样 **exit 0** → CI/脚本层面**不会报警** | `evaluate_rag_shadow.py:99,139` |
| 5 | **`loader` 完全不读 `validation_report` 的 `status`/`checks`/`errors`/`limitations`** —— 只取 `version` 字段（`loader.py:344-349`）。所以 v1_8 报告明确写着 `PASS_WITH_RELEASE_BLOCKERS`，loader 照样 `ready=True`。**"带发布阻断项"这个信号在运行时完全不可见** | `loader.py:325-352` |
| 6 | **`SHA256SUMS` 只校验清单里列出的文件** —— 多出来的文件不会被发现 | `loader.py:369-381` |
| 7 | **loader 的 v1_1 分支是死代码** —— 全仓无 `*enriched*` 资产，`_validate_v11_card()` / `_v11_content_hash()` 永不执行 | `loader.py:205-226, 468-497` |

### 3.11 `knowledge_degraded` 不是 RAG 标志（常见误读）

`ConsultResponse.knowledge_degraded` 的注释容易被读成"知识库降级"，实际语义是 **LLM provider 降级**：

```python
# consult_agent.py:1538 / 1638 / 1771
knowledge_degraded="knowledge_consult" in state.degraded_services
```

而 `knowledge_consult` 只在**生成模型不可用**时追加（`:959`、`:967`）。**它与 RAG 证据是否充分毫无关系。**

RAG 证据不足的真实表现是另外三条路径：

1. `rag_evidence == []` → prompt 里是空数组 `[]`
2. `rag_decision != sufficient` → prompt 追加兜底段（`knowledge_consult_client.py:243-250`）："知识库本轮没有检索到足够相关的参考证据。请忽略低相关检索结果，使用你掌握的通用宠物健康知识正常回答…不得把'没有知识卡'等同于'无法回答'"（有单测锁定：`tests/test_v73_rag_fallback_tone.py:64-77`）
3. `insufficient` **且** reason 含 `vague_general_query` **且** 无证据 **且** 无图片 → 强制清空病因并重写 summary（`consult_agent.py:1378`）
   > ⚠ 它**只认 `vague_general_query` 这一个 reason**；`low_relevance` / `ambiguous_elimination` **不触发**收口。

### 3.12 能力边界（诚实版）

| 文档暗示的能力 | 实际状态 |
|---|---|
| 向量召回 | ✅ 有（BGE-M3 dense，生产 compose 已挂 `/models/bge-m3`） |
| rerank 重排 | ❌ **不存在** |
| 引用溯源（用户可见） | ❌ `ConsultResponse` 无任何 citation / source 字段 ✅（`docs/RAG.md` 在此点上是**正确**的） |
| claim 级证据定位 | ❌ 仅 `page_or_section` 大致定位，且 309/311 条只到 Abstract |
| 卡片直答（fast answer） | ❌ 默认关闭，且 319 张卡中**仅 116 张**（`scope=simple_owner_question`）符合门禁 ✅ |
| 红旗复核 | ❌ 红旗只进 prompt |
| 品牌签审内容 | ❌ 319 卡 + 40 规则**全部** `production_eligible=false`、审核队列 359 条**全部 pending** ✅ |

**关于"卡片直答"的补充发现** ✅：`is_fast_answerable`（`retriever.py:427-430`）只放行 `scope` 为 `simple_owner_question` 或 `common_disease_health_education`。实测 v1_8 的 `scope` 只有两个取值：`common_disease_health_education_and_triage`（203 张）与 `simple_owner_question`（116 张）。**`common_disease_health_education` 这个值在 v1_8 中已不存在**（而且代码注释明确说 `_and_triage` 变体是刻意不放行的）。所以即使打开 `RAG_FAST_ANSWER`，直答池也只有 116/319。

---

## 4. 文档 vs 代码 一致性审计

| # | 文档声称 | 代码实际 | 严重度 |
|---|---|---|---|
| 1 | `state_machine.py` 在 `_execute()` 中做运行时校验，且有状态机测试 | 全仓零引用、零测试 | **高** |
| 2 | `docs/RAG.md`：生产强制 `RAG_MODE=off` | 无强制；生产模板是 `grounded` | **高** |
| 3 | `docs/RAG.md` 描述 v1_4/v1_5（260 卡/116 来源） | 运行时默认 v1_8（319 卡/131 来源） | **高** |
| 4 | `hybrid_retriever.py:3-6`：0.7/0.3+0.32 通过"拒绝探测 4/4" | 实测 v1_8 上 2/4 | **高** |
| 5 | `evaluate_rag_shadow.py` 的物种错配指标 | v1_8 分隔符变更导致指标失效（808 假错配） | **高** |
| 6 | `README.v1_8.md`：流程含 rerank、红旗复核 | 两者均不存在 | 中 |
| 7 | `docs/FLOWCHART.md`：源码行号锚点 | 全部失效（文件已 2212 行，锚点指向无关代码）✅ | 中 |
| 8 | `docs/FLOWCHART.md`：RAG 直答 "< 100ms" | 默认关闭（`RAG_FAST_ANSWER=false`） | 中 |
| 9 | `docs/LOCAL_MODEL_PLAN.md`：本地模型"规划中，尚未实现" | 早已落地（`LocalOpenAIAdapter` + Qwen3.5-9B） | 中 |
| 10 | `README.md:20`：单次问诊绝对 deadline **120 秒** | 代码默认 45s；`.env.example` 45s；`.env.docker.example` **80s** | 中 |
| 11 | `state_machine.py`/handoff：Vision 独立 **15s** 子预算 | `.env.docker.example` 是 **45s**（生产） | 中 |
| 12 | Prompt 版本 | 代码 docstring 2.1.0 / `PROMPT_VERSION` 2.5.0 / 冒烟日志硬编码 2.4.0 / RELEASE 2.4.0 | 低 |
| 13 | `docs/RAG.md:9-16`：v1_4 表格 260/40/116/300/0 | 只对 v1_4~v1_6 成立；v1_8 是 319/40/131/359 | 中 |
| 14 | `docs/RAG.md:29`：shadow 模式"只写日志，不影响回答和分诊" | 追问注入、prompt 分支、确定性收口三处不受模式约束 | **高** |
| 15 | `docs/RAG.md:37`："311 条证据记录中有 309 条只定位到 Abstract" | v1_4 实测 368/366；v1_8 实测 433/412 —— **对不上任何版本** ✅ | 中 |
| 16 | `docs/RAG.md:38`：平均每卡约 1.2 个来源 | v1_4 = 1.42，v1_8 = 1.36（低估） | 低 |
| 17 | `docs/RAG.md:40`："没有向量召回和 reranker" | 向量召回**有代码**（生产 compose 已启用）；reranker 确实没有 | 中 |
| 18 | `docs/RAG.md:41`："780 条种子问题" | v1_8 是 957 | 低 |
| 19 | `README.md:68` 把 `docs/RAG.md` 标为"RAG v1_8 状态" | 该文件标题是"# RAG v1.4 / v1.5 状态" | 低 |
| 20 | `docs/RAG.md:23-24`："运行时默认仍为 v1_4，待 v1.5 修复复审后再切换" | 运行时是 v1_8，且**切换机制已不存在**（全仓无 `RAG_INDEX_PATH` 配置） | 中 |
| 21 | `emergency_shadow.py:154-155`：未知物种只匹配兼容规则 | 代码物种未知时**完全不过滤**（`:166`） | 中 |
| 22 | `assets/rag/v1_7/validation_report` "新增55张高频主诉卡片" | 实测 304−260 = **44 张** | 低 |
| 23 | `README_DEPLOY.md:9` 宣传"shadow 模式 + fast 直答引用卡片" | 该组合在纯词法检索器下**必然 TypeError** | **高** |
| 24 | `docs/RAG.md:39`：每卡只有 2 条 `user_phrases` | v1_4 恰好 2.00；v1_8 实测 3.44 ✅ | 低 |
| 25 | `docs/RAG.md:42`：当前 API 响应没有完整的用户可见引用字段 | ✅ **准确** —— `RagHit.source_ids` 算了但无消费者，`ConsultResponse` 无 citation 字段 | — |
| 26 | `assets/rag/v1_8/README.v1_8.md:3-5`：319 卡 / 40 规则 / 131 来源 | ✅ **准确** | — |
| 27 | `knowledge_consult_client.py` 注释：`consult-model-text:8000` | compose 实为 `consult-vllm-text:8002` | 低 |
| 28 | 日志事件名 `deepseek_request` | 本地 vLLM 路径也用它（`:352`） | 低 |

---

## 5. 风险评估与建议

### 5.1 应立即修（正确性 / 安全）

| 优先级 | 问题 | 位置 | 建议 |
|---|---|---|---|
| P0 | 高风险生成失败返回 ERROR，绕过固定模板漏斗 | `consult_agent.py:958-978` | 失败分支增加 `if level == HIGH → build_fixed_safe_answer` |
| P0 | `ShadowRetriever.build_fast_answer` 签名不兼容（已复现 TypeError） | `retriever.py:285` vs `:1271` | 统一签名加 `query_species`；补直答路径测试。**注意 `README_DEPLOY.md:9` 恰好宣传"shadow 模式 + fast 直答"这个坏组合** |
| P0 | **v1_8 拒绝探针 4/4→2/4 回退**（同构对比，唯一变量是资产版本） | `retrieval_text` 重建 + `_QUERY_STOPWORDS` 不全 | 回滚 v1.8 把 `user_phrases` 拼进 `retrieval_text` 的做法，或补齐停用词表；重建后必须重跑 4 条探针 |
| P1 | 超时重试判定依赖中文字符串 | `consult_agent.py:118` | 引入 `TimeoutKind` 枚举或异常子类 |
| P1 | 陈旧向量缓存（只校验行数） | `hybrid_retriever.py:210-219` | 缓存 key 加入资产版本 + 模型标识 + 卡片内容哈希 |
| P1 | Worker 关停泄漏 | `dependencies.py:563-566` | 关停时遍历取消 `self._worker_tasks` |
| P1 | 多宠歧义 / 无幂等键急症不落历史 | `:872-879`、`:390-399` | 补 `_save_turn`（多宠路径需改 async） |
| P1 | **否定词表缺裸"不"，且缺陷同时存在于线上分诊引擎** | `rag/emergency_shadow.py:37` + `safety/emergency_rules.py:39` | 补 `不` 并加 `不…` 专项探针（现有探针只用"目前没有X"模板，测不出来） |
| P1 | `evaluate_rag_shadow.py` 硬编码成功 | `:99` | 改为按探针结果决定 `shadow_ready` 与退出码 |
| P1 | 向量生效性不可观测（`semantic_score` 恒 0、`embedding_model_version` 恒占位） | `retriever.py:219`、`hybrid_retriever.py:399`、`models.py:72` | 落真实 dense 分与模型标识，否则无法做 A/B 归因 |
| P2 | `validate_rag_assets.py:30` 对 v1_8 吞掉急症规则错误 | 默认资产上的校验静默失效 | 把 v1_8 纳入 |
| P2 | v1_8 缺席商用许可闸门 | `loader.py:317` | 纳入 v1_8（实测当前无违规，纯防御）；v1_8 尚有 41 条来源为 `legacy_license_metadata_reverification_pending` |
| P2 | `loader` 不读 `validation_report` 的 `status` | `loader.py:344-349` | 至少把 `PASS_WITH_RELEASE_BLOCKERS` 记入 warnings |
| P2 | `production_ready` 与 loader 闸门互斥、逻辑不可达 | `validate_rag_assets.py:56-60` vs `loader.py:417-418` | 明确"生产切换"流程并让脚本提示 |
| P2 | 生产 BGE-M3 跑 CPU，而阈值是在 RTX 5090 fp16 上调的 | `Dockerfile.api:29` + `hybrid_retriever.py:85-86` | 要么在生产环境重标定阈值，要么在 API 容器挂 GPU |
| P2 | 同一请求重复检索（直答预判 1 次 + agent 内 1 次） | `api/consult.py:267-271` + `consult_agent.py:756` | 复用首次 `RagResult` |
| P3 | 急症规则的 `model_constraints` 从未进入 prompt | `emergency_shadow.py:172-178` | 若规则资产要进生产，需设计注入路径 |

### 5.2 应尽快修（评测可信度）

1. **修 `evaluate_rag_shadow.py:48` 的分隔符**：改用 `re.split(r"[|/]", row["species"])[0]`，并重跑 v1_8 基线。
2. **重建基准集**：当前 957 条种子题全部自指。`docs/RAG.md:46` 的发布门槛要求"至少 500 条、建议 1000 条真实脱敏问题做分层盲测"—— 这才是唯一能给出真实召回率的做法。
3. **把拒绝探测扩成真正的负样本集**（当前仅 4 条手工 probe，且 2/4 不过）。这是目前**唯一**能发现"该拒答却答了"的护栏。

### 5.3 文档治理

1. **删除或冻结 `state_machine.py`** —— 保留会造成"有运行时校验"的错觉。要么真正接入（在 `_execute` 各阶段边界调 `assert_transition`）+ 补测试，要么降级为 `docs/` 下的图。
2. **`docs/RAG.md` 整体重写**指向 v1_8，修正"生产强制 off"和资产计数；把 `validation_report.v1_8.json` 声明为唯一真相（沿用 `TRUTH_REPAIR_REPORT.md` 的"唯一副本"约定）。
3. **修 `docs/FLOWCHART.md` 的行号锚点**（或去掉行号只留阶段名），并标注哪些能力默认关闭。
4. **删除 `docs/LOCAL_MODEL_PLAN.md` 或标注"已实现，见 handoff §4.1"**。
5. **统一 README 的 120s / 45s / 80s**，明确区分"代码默认 / 模板 / 部署实测"。
6. **删除 `consult_agent.py.original`**（源码包内的备份文件）。
7. 在 `README.v1_8.md` 的推荐流程中标注 rerank / 红旗复核为 **尚未实现**。

### 5.4 架构层面值得指出的两点

**(1) "Agent" 这个词在这个项目里名不副实，但这不是缺点。** 医疗分诊场景下，固定流水线 + 确定性漏斗 + 绝对 deadline 是**正确选择**：可复现、可审计、可解释、无无限循环风险。真正需要警惕的是**文档把它描述成状态机驱动**，从而让人误以为有运行时校验。建议统一措辞为"确定性分诊编排"。

**(2) RAG 的诚实度值得肯定，也值得保护。** `validation_report.v1_8.json` 明确写 `PASS_WITH_RELEASE_BLOCKERS`、`production_eligible=0`、承认来源许可未核验、承认种子题自指；`loader.py` 用哈希 + 审核队列把"未签审不得发布"做成了**代码强约束**而非文档约定。这套机制比很多同类项目做得严格。**风险恰恰在于**：正因为闸门严格，团队倾向于用"资产校验通过"替代"临床有效性验证"—— 而 `rejection_probes 2/4` 说明后者仍是空白。

---

## 6. 复现命令

```powershell
# 资产校验（实测：shadow_ready=true, production_ready=false, errors=[]）
.\.venv\Scripts\python.exe scripts\validate_rag_assets.py

# 评测（实测：recall@1=0.9896, species_mismatches=808, rejection 2/4）
.\.venv\Scripts\python.exe scripts\evaluate_rag_shadow.py

# 测试（实测：47 passed in 1.70s；无任何 state_machine 用例）
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 附：关键配置默认值速查

| 配置 | 代码默认 | 生产模板 | 说明 |
|---|---|---|---|
| `rag_mode` | `shadow` | `grounded` | 生产实际 grounded |
| `rag_index_path` | `""` → `assets/rag/v1_8` | 同 | |
| `rag_top_k` / `rag_score_threshold` | 4 / 0.24 | 同 | |
| `rag_hybrid_alpha` / `rag_hybrid_threshold` | 0.7 / 0.32 | 同 | |
| `rag_embedding_model_path` | `""`（→ 纯词面） | `/models/bge-m3` | compose 硬编码 |
| `rag_fast_answer` | **False** | **False** | 直答通道关闭 |
| `rag_followup_check` | True | True | 追问查缺开启 |
| `rag_emergency_shadow` | True | True | 影子匹配开启 |
| `consult_total_timeout_seconds` | 45 | **80** | README 称 120 ❌ |
| `vision_timeout_seconds` | 15 | **45** | |
| `safety_rewrite_timeout_seconds` | 15 | 15 | |
| `knowledge_max_tokens` | 0（不限） | 未设 | 只受 deadline 约束 |
| `knowledge_local_temperature` | 0.6 | 0.6 | |
| `mock_vision` | True | false | 影响物种冲突检测 |
| BGE-M3 运行设备 | CPU（`torch.cuda.is_available()` 判定） | **CPU**（`Dockerfile.api:29` 装 CPU torch，API 容器无 GPU 预留） | 阈值是在 RTX 5090 fp16 上调的 |

---

## 附录 A：常见误读纠正 —— 为什么"五阶段"式理解不成立

一个很自然的误读是把下面 5 个名字当成流水线的 5 个阶段：

```
ConsultCommand → KnowledgeConsult → VetRecommendation → GenerationConsult → ConsultResponse
```

**实际身份**（全部经 grep 核实）：

| 名字 | 真实身份 | 位置 | 是不是阶段 |
|---|---|---|---|
| `ConsultCommand` | **输入数据模型**（路由层构造的产物） | `schemas/consult.py:49` | ❌ |
| `KnowledgeConsultService` | **LLM 调用封装**（唯一职责：序列化 state → 调 adapter） | `services/knowledge_consult_service.py:19` | ❌ 末端执行器 |
| `VetRecommendation` | **输出数据模型**，LLM 产物里的一个字段 | `schemas/consult.py:254` | ❌ 是结果，不是引擎 |
| `GenerationConsult` | **不存在**（全仓零命中）；实际是 `GeneratedConsultation`（输出数据模型） | `schemas/consult.py:305` | — |
| `ConsultResponse` | **最终响应数据模型** | `schemas/consult.py:408` | ❌ 出口 |

5 个名字里 4 个存在但都是**数据容器**，第 5 个根本不存在。

**判断"某组件是不是阶段"只需两个测试**：
1. 它是否被 `ConsultAgent._execute()` **顺序调用**？
2. 它是否**写入 `ConsultState` 字段**？

按这两个测试，**整个系统只有一个流程：`ConsultAgent._execute()`（`consult_agent.py:549-1144`）**；其余名字要么是它的输入/输出数据模型，要么是它调用的单一职责工具。

### 逐条对照

| 误读 | 实际 | 证据 |
|---|---|---|
| `ConsultCommand` 用来"获取路由 API" | 方向反了：`api/consult.py` 解析 multipart → 构造 `ConsultCommand` → 传给 `agent.run()`。它是**产物** | `consult.py:50` "agent.run() 的输入（路由层协议转换后）" |
| `KnowledgeConsultService` 判断消息能否过审 | 过审是 `ModerationService.check_input()`（规则 + Guard 模型） | `moderation_service.py:38` |
| 同上，判断文本/图片是否存在 | API 层 multipart 校验 + `CompletenessChecker.evaluate()` | `completeness_checker.py:145-252` |
| 同上，判断 RAG 能否启动 | 不是"判断"，是**进程启动时由配置决定**（`if s.rag_shadow`）。且检索是**旁路**：`except Exception: logger.warning("rag_failed")`，失败不阻断也不决定任何事 | `dependencies.py:179`、`consult_agent.py:787-788` |
| `VetRecommendation` 根据紧急程度获取医疗评估 | 它是 LLM 生成、后被 `MedicalSafetyChecker` **校验**的字段 → **后置** | `medical_checker.py:87-104` |
| "医疗评估"发生在生成前 | 发生在生成**之后**。生成前是 `EmergencyRuleEngine` + `RiskEngine` 定风险；生成后是 `MedicalSafetyService.review()` 校验 `vet_recommendation` 是否匹配上游风险 | `consult_agent.py:829-836` vs `:984-1064` |
| 把 KnowledgeConsult 的内容传给 GenerationConsult 的 LLM | `KnowledgeConsultService` **本身就是调 LLM 的那一层**，不存在两级生成。传进 LLM 的是 state 的上游产物 | `knowledge_consult_service.py:26-58` |
| `ConsultResponse` 用来"调整结构化输出" | 调整在**更早的医疗审核阶段**（`repair_locally` + `clean_owner_facing_language`），之后才由 `_answer()` 组装 | `consult_agent.py:992-1116` → `:1142` |
| 最后"把输出结果传给后端" | **这个服务就是后端**。调用方是公网业务网关（`:19000`），经 JSON/SSE 返回 | `docs/ARCHITECTURE.md:25-26` |

**唯一侥幸正确的地方**：误读中"风险评估在生成之前""结构化输出在最后"这两个**位置**是对的；错的是**由谁做**。

### 被完全遗漏的真实阶段

1. **文字急症预判**（纯规则、零外部依赖，位于一切模型调用之前）——`:597-600`
2. 会话历史加载（Redis，失败降级为单轮）
3. **图片视觉分析**（VisionGateway → Qwen3.5-4B）——`:671-732`
4. 非宠物问诊短路（天气/气温/下雨三条正则）
5. 完整度判断 → 决定追问 / provisional / 直答
6. 风险聚合（只升不降，专科仅覆盖眼科）
7. 生成三模式（normal / provisional / urgent_guidance）
8. **医疗安全审核 → 本地修复 → 重写一次 → 固定模板兜底**（四级收敛）
9. 输出审核（Guard）
10. 组装响应 + 存历史 + 对话存档

另有一层完全在误读视野之外：**队列化**（PG `consult_task` + `consult_outbox` 同事务 → RocketMQ → Worker 并发消费），这改变了整个调用形态（`docs/ARCHITECTURE.md:13-15`）。

### 正确的一句话描述

> **`ConsultAgent._execute()` 按固定顺序推进：文字急症预判 → 加载历史 → 输入审核 → 图片分析 → RAG 检索（旁路）→ 完整度 → 风险聚合 → LLM 生成（三模式）→ 医疗审核与收敛兜底 → 输出审核 → 组装 `ConsultResponse`。** 全程中间产物写入共享的 `ConsultState`；`ConsultCommand` / `GeneratedConsultation` / `ConsultResponse` 只是这条链路的输入、中间与输出数据模型。

### 为什么会误读

这组命名有强误导性：
- `VetRecommendation` 定义在 `consult.py` 的"模型生成产物"区块**之前**，视觉上像是输入模型
- `GeneratedConsultation` 的 docstring 写"由 KnowledgeConsultService 生成" → 容易读成"KnowledgeConsult 是判断层，另有 Generation 层"
- 4/5 的名字真实存在，诱导人**按名字反推流程**，而不是按 `_execute()` 的调用顺序读流程
