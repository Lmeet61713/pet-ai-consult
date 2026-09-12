# pet-consult v7.3 —— Agent 与 RAG 准入测试 · 完整答案

配套文档：`ANALYSIS-AGENT-RAG.md`（审计报告 + 附录 A 心智模型纠正）
所有 `文件:行号` 均为实际核实，非引用文档。

---

## 第一轮 · 定位

### Q1 这个系统是不是 Agent？准确描述它是什么。

**不是 Agent。** 全仓 grep `tools` / `function_call` / `tool_choice` **零命中**；没有"模型决定下一步"的循环。

准确描述：**确定性分诊编排流水线（deterministic triage orchestrator）**——`ConsultAgent._execute()`（`consult_agent.py:549-1144`，约 600 行）按固定顺序推进 13 个阶段，所有分支都是预定义的 Python `if/elif/return`。

模块自己承认了（`state_machine.py:18-20`）：

> 固定状态机：不会自己拆解任务、自由调用工具的通用 Agent。它的"智能"体现在：何时降级、何时追问、何时走固定模板；所有状态转移都是预定义的，没有运行时动态决策。

**也**不是文档说的"状态机驱动"——`state_machine.py` 是死代码（见 Q19）。

> ⚠ 不要叫它"Chat 聊天机器人"。Chat bot 没有：确定性急症分诊、四维风险分级、四级安全收敛、绝对 deadline、固定安全模板、PG+Outbox+RocketMQ 队列。叫法错误会导致低估安全约束强度。
>
> 在医疗分诊场景下，固定流水线是**正确选择**：可复现、可审计、可解释、无无限循环风险。

### Q2 主流程阶段顺序

真实顺序（`consult_agent.py:549-1144`）：

| # | 阶段 | 行号 | 主要产物 |
|---|---|---|---|
| 1 | **文字急症预判**（纯规则，零外部依赖） | `:597-600` | `text_emergency_precheck` |
| 2 | 加载会话历史（Redis，失败降级单轮） | `:603-609` | `history` / `history_summary` |
| — | 物种推断 | `:612` | `pet_info` |
| 3 | **输入审核**（规则 + Guard 模型） | `:626-648` | `input_moderation` |
| 4 | **非宠物问诊短路**（天气/气温/下雨） | `:652-668` | 直接返回固定文案 |
| 5 | **图片视觉分析**（VisionGateway → Qwen3.5-4B） | `:671-732` | `vision_findings` |
| 6 | 图文物种冲突 / 无宠物检测 | `:746-748` | `warnings` / 清空 findings |
| 7 | **RAG 检索**（旁路，异常被吞） | `:750-789` | `rag_result` / `rag_evidence` / `rag_questions` |
| 8 | **完整度判断** | `:791-826` | `completeness` |
| 9 | **急症规则 + 风险聚合** | `:828-839` | `emergency_result` / `risk_result` |
| — | EMERGENCY 短路（先存历史） | `:840-849` | 固定急症模板 |
| — | 卡片直答（**默认关闭**） | `:851-868` | `_fast_answer` |
| — | 多宠歧义固定追问 | `:870-879` | （**不存历史**） |
| 10 | **生成**（三模式：normal/provisional/urgent_guidance） | `:881-980` | `generated` |
| 11 | **医疗审核 + 四级收敛兜底** | `:982-1081` | `medical_review` |
| 12 | **输出审核**（Guard） | `:1118-1139` | `output_moderation` |
| 13 | 组装响应 + 存历史 | `:1141-1144` | `ConsultResponse` |

**最常被漏掉的三个**：输入审核（3）、风险聚合（9）、输出审核（12）。

### Q3 `ConsultState` 是什么？举 3 个字段和写入者。

`state.py:83-371`，Pydantic 模型，充当**单次问诊生命周期内的"状态总线 / 黑板"**。所有阶段共享同一个引用：读上游产物、写自己的产物。

设计三原则（`state.py:13-24`）：
1. **单一数据源**——无隐式全局变量，任何中间产物都通过 state 传递
2. **阶段回写模式**——每个阶段只做两件事：读上游字段、写自己的字段
3. **反向隔离**——`from_command()` 从 `ConsultCommand` 提取核心字段，后续服务只依赖 `ConsultState`，不依赖 API 层

字段 + 写入者示例：

| 字段 | 写入者 | 位置 |
|---|---|---|
| `vision_findings` | `image_service.analyze()` | `:681-687`（异常/冲突时被清空 `:694/705/816/826`） |
| `risk_result` | `risk_engine.evaluate(state)` | `:836` |
| `generated` | `consultation_service.generate / generate_provisional / generate_urgent_guidance` | `:890 / 899 / 904` |
| `case_facts` | **副作用**：`CompletenessChecker.evaluate()` 内部调 `FollowUpTracker.extract()` | `completeness_checker.py:176` |
| `rag_result` / `rag_evidence` | `rag_retriever.search()` / `build_grounded_evidence()` | `:756-763` |
| `completeness` | `completeness_checker.evaluate()` | `:792-794` |
| `input_moderation` | `moderation.check_input()` | `:632-636` |
| `degraded_services` | 各降级分支 | `:414, 457, 608, 692, 703, 824, 912, 959, 967, 1053` |

> ⚠ **死字段**：`state.status`（`state.py:305`）**从未被赋值**。状态只在 `ConsultResponse.status`。
> ⚠ **未声明的动态属性**：`_steps`（`:594`）、`_species_source`、`_generate_retried` —— Pydantic v2 对下划线名走 `_object_setattr`，能跑但不参与校验/序列化。

### Q4 怎么判断某个类是不是流程阶段？

**两个测试**：
1. 它是否被 `ConsultAgent._execute()` **顺序调用**？（去 `consult_agent.py` grep 调用点）
2. 它是否**写入 `ConsultState` 字段**？

两者皆否则不是阶段。

按此测试，**整个系统只有一个流程：`_execute()`**。其余名字要么是输入/输出**数据模型**，要么是它调用的**单一职责工具**：

| 名字 | 真实身份 | 位置 |
|---|---|---|
| `ConsultCommand` | 输入数据模型（路由层构造的产物） | `schemas/consult.py:49` |
| `GeneratedConsultation` | 输出数据模型（LLM 产物） | `schemas/consult.py:305` |
| `ConsultResponse` | 最终响应数据模型 | `schemas/consult.py:408` |
| `VetRecommendation` | 输出数据模型（LLM 产物里的一个字段） | `schemas/consult.py:254` |
| `KnowledgeConsultService` | LLM 调用封装（序列化 state → 调 adapter） | `services/knowledge_consult_service.py:19` |
| `ModerationService` | 输入/输出内容审核工具 | `services/moderation_service.py:38` |
| `RiskEngine` | 风险聚合工具 | `agent/risk_engine.py:41` |

> `GenerationConsult` **不存在**（全仓零命中）——正确名字是 `GeneratedConsultation`。

---

## 第二轮 · 安全关键路径（红线）

### Q5 哪些情况绕过大模型直接回答？

至少 11 条：

| # | 条件 | 行号 | 响应 |
|---|---|---|---|
| 1 | 文字急症预判命中 + **无幂等键** | `:390-399` | `_fixed_urgent_response`（**不存历史、不存档**） |
| 2 | `risk_result.level == EMERGENCY` | `:840-849` | 固定急症模板（**先存历史**） |
| 3 | 输入审核 `should_refuse_medical_request` | `:645-647` | `_refuse`（REFUSE） |
| 4 | 输入审核 `parse_ok is False` | `:639-644` | 急症→固定模板；否则 `_review` |
| 5 | 非宠物问诊（天气/气温/下雨三条正则） | `:652-668` | `_fixed_out_of_scope_response` |
| 6 | 多宠歧义 `pet_ambiguous` | `:870-879` | `_fixed_pet_ambiguous_response` |
| 7 | 纯图无文字 + vision 失败 | `:742-744` | `_review(reason="image_unavailable")` |
| 8 | 医疗审核 + 修复 + 重写都不过 | `:1063-1064` | `build_fixed_safe_answer` |
| 9 | 流式增量审核命中违规 | `:910-918` | `build_fixed_safe_answer` |
| 10 | 输出审核 `blocked` | `:1132-1133` | `_review` |
| 11 | （卡片直答） | `:851-868` | `_fast_answer` —— **默认关闭** ⚠ |

另：**任何阶段抛异常且 `_precheck_urgent()` 为真** → 固定急症模板（`:642 / 498 / 507 / 515`）。

### Q6 文字急症判定在流程哪一步？为什么必须在那个位置？

**第一步。** `_run_impl:387-389`（比 `_execute` 更早），`_execute:597-600` 为兜底。

三个原因：
1. **纯规则、零外部依赖**——`emergency_rules.precheck_text()`（`safety/emergency_rules.py:106-125`）只查文字 + 宠物档案，不碰 Redis / PG / LLM / 视觉。**所以即使所有依赖全挂，急症处置仍能返回。**
2. **必须在任何模型调用之前**——否则急症会被排队、超时、模型不可用拖死。
3. **成为全局安全阀**——命中后，后续任何阶段失败都返回固定急症模板（`:642/498/507/515`）。

### Q7 医疗审核不通过后的完整收敛路径？几级？

**四级**（`consult_agent.py:982-1081`）：

```
生成 → ①医疗审核
        ↓ 不通过
     ②repair_locally（本地确定性修复）→ 复审
        ↓ 仍不通过且有预算
     ③rewrite_once（携带违规清单，仅一次）→ 复审
        ↓ 重写后仍不通过
     ④repair_locally（二次）→ 复审
        ↓ 仍不通过
     ⑤build_fixed_safe_answer（固定安全模板兜底）
```

| 级 | 机制 | 位置 | 说明 |
|---|---|---|---|
| 1 | `repair_locally` | `:992-1019` | **只改违规字段**：软化确诊断言、补免责、删矛盾处置、删眼部危险条目、上调 risk/urgency。保留其余内容 |
| 2 | `rewrite_once` | `:1020-1051` | 带 `rewrite_violations` + `rewrite_source`，受 `deadline.has_remaining(safety_rewrite_timeout_seconds)` 约束，**仅一次**防死循环 |
| 3 | `repair_locally`（二次） | `:1033-1051` | 重写后仍不过则再本地修复 |
| 4 | `build_fixed_safe_answer` | `:1063-1064` | 固定模板（EMERGENCY 时内部升级为急症模板，`medical_safety_service.py:419-420`） |

**设计要点**：本地修复优先于模型重写——省时，且不丢已正确的针对性内容（`repair_locally` 只清空 `answer_text` 强制重渲染，其余字段保留）。

### Q8 生成模型不可用时，HIGH 风险病例会得到什么响应？合理吗？

**实际行为：`status=error`，拿不到任何医疗建议。不合理 —— 这是 P0 缺陷。**

```python
# consult_agent.py:958-965（重试后仍失败）
except KnowledgeConsultUnavailable as exc2:
    state.degraded_services.append("knowledge_consult")
    state.warnings.append(str(exc2))
    return self._service_unavailable(state, str(exc2))   # → ERROR

# consult_agent.py:966-978（不可重试）
else:
    state.degraded_services.append("knowledge_consult")
    return self._service_unavailable(state, str(exc))    # → ERROR
```

两条失败路径**既不判断 `risk_result.level == HIGH`，也不调 `build_fixed_safe_answer`**。

**为什么这是 bug**：
- `:884-893` 已明确为这个病例选了 `urgent_guidance` 模式 → 说明系统"知道"它高风险
- 与项目自己声明的"回答优先 / 高风险不短路"原则矛盾
- 让 Q7 的四级漏斗对高风险病例**完全失效**——因为根本走不到漏斗
- 对外表现：高风险病例收到 `status=error` + `KNOWLEDGE_CONSULT_UNAVAILABLE`，**无任何就医建议**

**修法**：
```python
if state.risk_result and state.risk_result.level in (RiskLevel.HIGH, RiskLevel.EMERGENCY):
    state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
    # 继续走输出审核(:1118) 与组装(:1141)，而非 return ERROR
```

### Q9 生成失败后如何决定重试？依赖什么？隐患？

```python
# consult_agent.py:98-118
def _should_retry_knowledge_failure(exc: KnowledgeConsultUnavailable) -> bool:
    return "超时" not in str(exc)
```

配合 `:922-926`：
```python
if (_should_retry_knowledge_failure(exc)
    and not getattr(state, "_generate_retried", False)
    and deadline.has_remaining(12.0)):
    state._generate_retried = True
    # → deadline.child(cap=15.0) 重试一次，模式与主分支一致
```

**决策依据是对异常消息做中文字符串子串匹配**，依赖 `knowledge_consult_client.py:114 / 177 / 384 / 464` 恰好抛出 `"知识问诊超时"`。

**三个隐患**：
1. **跨模块隐式字符串契约**——无共享常量，任何一处理措辞改动会**静默失效**掉"防重试风暴"保护
2. 只覆盖 httpx 超时。`RequestDeadlineExceeded` 是另一类异常，被 `KnowledgeConsultService:73` 包装成 `"知识问诊调用失败: 请求已超过处理时限"`（**不含"超时"**）→ 被误判为可重试，仅靠 `has_remaining(12.0)` 拦住
3. **不可配**：`12.0` / `15.0` / `20.0` / `0.1` 都是硬编码常量

**正确做法**：引入 `TimeoutKind` 枚举或异常子类层级，按类型而非文本判定。

**为什么"超时不重试"重要**：模型超时 = 容量已饱和的信号。立刻重试会把已饱和模型的请求量瞬间翻倍，形成重试风暴。

---

## 第三轮 · RAG

### Q10 运行时默认加载哪个资产版本？哪一行决定？

**`assets/rag/v1_8`。**

决定链：
```python
# config.py:102 —— 默认是空串，不是路径
rag_index_path: str = ""

# dependencies.py:181-185 —— 空则硬编码兜底
asset_path = s.rag_index_path
if not asset_path:
    asset_path = str(Path(__file__).resolve().parents[2] / "assets" / "rag" / "v1_8")
```

全仓 `.env` / `.env.example` / `compose.yaml` / `Dockerfile.api` **都没有设置 `RAG_INDEX_PATH`** → 实际加载 v1_8。同理 `RAG_EMBEDDING_MODEL_PATH` 只在 `compose.yaml:249` 设为 `/models/bge-m3`。

**资产版本谱系（实测计数）**：

| 版本 | 知识卡 | 急症规则 | 来源 | 基准种子 | 审核队列 | 类别 |
|---|---:|---:|---:|---:|---:|---:|
| v1_4 | 260 | 40 | 116 | 780 | 300 | 27 |
| v1_5 | 260 | 40 | 116 | 778 | 300 | 27 |
| v1_6 | 260 | 40 | 116 | 778 | 300 | 27 |
| v1_7 | 304 | 40 | 116 | 912 | 344 | 27 |
| **v1_8** | **319** | **40** | **131** | **957** | **359** | **27** |

全部版本 `production_eligible` 真值数 = **0**。

**文件格式**（这是容易搞错的点）：
- 知识卡片 → **JSONL**（`knowledge_cards.combined_v1_8.jsonl`）
- 急症规则 → **JSON**（`emergency_rules.combined_v1_8.json`）
- 来源清单 → **JSON**（`sources.combined_v1_8.json`）
- 基准种子 / 审核队列 / 修复清单 → **CSV**

### Q11 检索的打分公式？各项和权重？

**词法打分**（`retriever.py:194-201`，两个检索器共用）：

```python
final_score = min(1.0,
      keyword_score * 0.72     # retrieval_text 的 CJK 2-gram 重叠率
    + title_score   * 0.12     # 标题重叠率
    + phrase_score  * 0.20     # user_phrases 命中比例
    + species_score            # 0.08，仅当物种已知
    + category_score)          # 0.04，仅当调用方传 category
```

- **`min(1.0, …)` 是唯一归一化** —— 加法封顶，不是加权平均
- 词法阈值 **0.24**，`top_k` **4**
- `_overlap = |Q∩C| / |Q|`（`retriever.py:303-306`）—— **按查询侧归一，是 containment 不是 Jaccard**（代码注释写 "Jaccard-like" 是误导）
- 词法化（`:290-300`）：CJK **整段串**（如 `狗正常吃饭喝水`）+ 全部 **2-gram** + `[a-z0-9_]+`。无 3-gram、无分词器、无同义词、无拼音
- 候选门禁（`:192-193`）：`keyword<=0 and phrase<=0 and category<=0` → 丢弃。**物种分本身不能让卡片进候选**（防跨病种污染）

**⚠ `category` 是死参数**：`consult_agent.py:756-759` 只传 `species=`，**从不传 `category=`** → 生产链路里 `category_score ≡ 0`。

**混合打分**（`hybrid_retriever.py`）：

```python
final_score = alpha * lexical + (1 - alpha) * dense   # alpha = 0.7
```

- 阈值 **0.32**（纯词法回退时用 **0.24**，见 `effective_threshold():183-186`）
- 物种未知时阈值放宽 `0.08 * alpha`（`:407-409`）
- **两阶段**：词面粗筛全量卡片 → 取 `coarse_top_k=30` → dense 精排（`:337-385`）
- 融合是**加权和，不是 RRF**

**查询前置门禁**（命中即 `INSUFFICIENT`，不进入打分）：
- `is_vague_general_query`——"没精神/状态不好"等无具体症状（`:337-344`）
- `is_ambiguous_elimination_query`——"上厕所"未区分排尿/排便（`:347-353`）
- `is_cat_chin_specific_query`——猫下巴皮损（`:356-366`）
- `_POLICY_TERMS`（剂量/mg/停药/换药/处方）→ **`POLICY_RESTRICTED`**（`:32, 232-234`）

### Q12 有没有 reranker？有没有真正的向量召回？生产跑在什么设备？

**① reranker：没有。** 全仓 grep `rerank|CrossEncoder|BM25|faiss|pgvector` **零命中**。`README.v1_8.md:18` 推荐的流程里写了 "rerank" —— **该功能不存在**。融合是 alpha 加权和，不是 RRF。

**② 向量召回：有代码，但要打折。**
- BGE-M3 dense **1024 维**、L2 归一化，`dense = card_emb @ q_vec[0]`（`hybrid_retriever.py:333`）
- **但 dense 只对词面粗筛出的 ≤30 张候选精排**（`:372-378`）→ **向量救不回词面漏掉的卡片**
- 无 ANN 索引（无 FAISS/pgvector），O(N) 全量矩阵乘法
- 卡片向量缓存在 `runtime/bge_card_emb.npy`（`:44`）
- 后台线程加载，索引就绪前自动回退纯词面（`:231-254`）

**③ 生产设备：CPU（不是"linux 服务器"这种答法）。**
```python
# hybrid_retriever.py:85-86
use_fp16 = torch.cuda.is_available()
device = "cuda" if use_fp16 else "cpu"
```
- `Dockerfile.api:29` 装的是 **`torch-2.13.0+cpu`**（Dockerfile 第 3 行注释也写明 "BGE-M3(FlagEmbedding + CPU torch)"）
- `consult-api-1` 在 compose 里**无 GPU 预留**（`compose.yaml:216-274`）
- → 容器内 `cuda.is_available() == False` → **CPU float32**

> ⚠ **而 `hybrid_retriever.py:3` 标注 A/B 结论是 "2026-08-15, AutoDL RTX 5090"（GPU fp16）** → 0.32 阈值是在 GPU fp16 下选的，**与生产环境的 dense 分分布不同**。

**④ 生效性不可观测**（两个致命缺口）：
- `semantic_score` 恒为 `0.0`（`retriever.py:219`、`hybrid_retriever.py:399`）——dense 分算了但不落到结果里
- `embedding_model_version` 恒为 `"none-lexical-shadow"`（`models.py:72`）——**全仓无任何赋值点**
- → **BGE-M3 是否真的生效，从日志和响应里完全无法验证**

**⑤ `warmup()` 无条件返回 `False`**（`:254`）→ `dependencies.py:205-212` 的"预热完成"分支是**不可达代码**，日志永远打印"后台预热中"。

### Q13 `loader.py` 用什么机制保证"未签审的内容不得进入服务"？

`RagAssetLoader.load()`（`loader.py:86-146`）执行**六道静态闸门**，任一失败累积错误码；`report.ready = bool(cards) and not errors`（`:52-55`），`ready=False` → 检索器直接返回 `UNAVAILABLE`，**绝不带病提供知识**。

| # | 闸门 | 位置 | 作用 |
|---|---|---|---|
| 1 | **SHA256SUMS 整目录校验** | `:354-381` | 文件被篡改/损坏即报错 |
| 2 | **逐条 `content_hash` 重算比对** | `:263-267` | `_v14_content_hash()`：剔除 `content_hash` 后紧凑 JSON 序列化再 SHA-256（**不排序键**，保持资产产出顺序） |
| 3 | **审核队列哈希一致** ⭐ | `:268-269` | `review_hashes[card_id] == digest` —— **证明"送审版本 == 加载版本"逐字节相同**。这是最精巧的一道 |
| 4 | **发布闸门** | `:405-418` | `index_tier == "test"` 且 `production_eligible is False`（否则 `non_test_record` / `production_eligible_record`） |
| 5 | **审核状态** | `:260-262` | `veterinary_review.status == "pending"` |
| 6 | **证据可溯源** | `:270-275` | `source_id` 必须在来源清单 + **必须带 `page_or_section` 定位** |

**设计评价**：把"未签审不得发布"做成**代码强约束而非文档约定**——这比多数同类项目严格，是这份交付里最值得保留的部分。

**⚠ 但校验能力有几个边界**：
- **只校验不修复**（`repair_manifest.*` 是离线产物，loader 不读）
- **无缓存**——每次 `load()` 重读文件 + 重算 SHA-256（实测 v1_4 18ms / v1_8 26.5ms）
- `SHA256SUMS` **只校验清单里列出的文件**，多出来的文件不会被发现（`:369-381`）
- 设计上不抛异常（`:99-100` 明说"保证服务可启动"），但 `read_text()` 编码错误、`csv.DictReader` 缺列、`read_bytes()` 内存错误等路径**无保护，会真抛**
- **完全不读 `validation_report` 的 `status`/`checks`/`errors`/`limitations`**（`:325-352` 只取 `version`）→ v1_8 报告明确写着 `PASS_WITH_RELEASE_BLOCKERS`，loader 照样 `ready=True`

### Q14 当前默认版本在这套闸门里有什么缺口？

**`loader.py:317`，商用许可闸门把 v1_8 排除在外**：

```python
if asset_format in ("v1_4", "v1_5", "v1_6", "v1_7") and (
    source.get("commercial_use_allowed") is not True
    or "NC" in str(source.get("license", "")).upper()
    or "ND" in str(source.get("license", "")).upper()
):
    errors.append(f"disallowed_source:{source_id}")
```

**v1_8 正是运行时默认版本，却享受最弱的知识产权闸门。**

**实测结论**：v1_8 的 131 条来源**当前全部合规**（`commercial_use_allowed=true`，无 NC/ND，`license` 字段齐全）→ **无实际违规**。但：
- 闸门已不再保护默认资产版本 → 任何后续修改/新版本同样不受保护
- v1_8 还有 **41 条来源**为 `legacy_license_metadata_reverification_pending`（与 `validation_report.v1_8.json` 的 `legacy_sources_pending_license_metadata_reverification: 41` 一致）
- 这正是 v1_8 校验报告自己承认的限制："仍有部分 V1.7 遗留来源缺少结构化许可核验状态，已如实标为待复核，未伪造为已验证"

**其他相关缺口**：
- `validate_rag_assets.py:30` 同样只对 v1_4~v1_7 合并急症规则错误 → **v1_8 的急症规则错误被静默吞掉**
- `production_ready` 逻辑不可达：要求全部 `production_eligible=True`，但那必然触发 loader 的 `production_eligible_record` 错误 → **两个闸门互相排斥**（`validate_rag_assets.py:56-60` vs `loader.py:417-418`）

---

## 第四轮 · 陷阱题

### Q15 把 `RAG_FAST_ANSWER` 改成 `true` 会发生什么？

**在纯词法检索器下必然抛 `TypeError` → 整个请求 `INTERNAL_ERROR`。**

```python
# consult_agent.py:1271-1276 —— 调用方传了 query_species
payload = self.rag_retriever.build_fast_answer(
    state.rag_result,
    query_species=(normalize_species(state.pet_info.species) if state.pet_info else None),
)

# rag/retriever.py:285 —— 定义不接受该参数
def build_fast_answer(self, result: RagResult) -> dict:

# rag/hybrid_retriever.py:461 —— 只有 Hybrid 版接受
def build_fast_answer(self, result: RagResult, query_species: str | None = None) -> dict:
```

**触发条件**：`RAG_EMBEDDING_MODEL_PATH` 为空（→ `dependencies.py:217` 构造 `ShadowRetriever`）**且** `RAG_FAST_ANSWER=true`。

该 `TypeError` **不在任何 `try` 内** → 冒泡到 `_run_impl:513` 的全局 `except Exception` → `INTERNAL_ERROR`。**每一次可直答请求都返回错误。**

> 讽刺的是 `README_DEPLOY.md:9` 恰好宣传"shadow 模式 + fast 直答引用卡片"这个组合。
> 注意 `dependencies.py:374` 的 worker 直答路径调用的是 `build_fast_answer(rag_result)`（不带 kwarg），**所以只有 agent 路径坏**。

**即使修好签名，直答池也很小**：

`is_fast_answerable`（`retriever.py:427-430`）只放行：
```python
return scope == "simple_owner_question" or scope == "common_disease_health_education"
```
实测 v1_8 只有两个 scope 值：`common_disease_health_education_and_triage`(203) 和 `simple_owner_question`(116)。**`common_disease_health_education` 这个值在 v1_4~v1_8 全部资产中都不存在** → 该 `or` 分支是**死代码**。

→ 实际可直答的只有 **116 / 319 张卡（36%）**。（代码注释还明确说 `_and_triage` 变体是 2026-08-19 刻意不放行的。）

### Q16 `RAG_INDEX_PATH` 从 v1_5 切到 v1_6 有什么隐藏风险？

**陈旧向量缓存会静默复用错误向量。**

```python
# hybrid_retriever.py:210-219
if cache.exists():
    arr = np.load(cache)
    if arr.shape[0] == len(texts):     # ← 只校验行数！
        self._card_emb = arr
        logger.info("BGE 卡片向量缓存命中: %s", cache)
        return self._card_emb
```

缓存有效性**只校验 `arr.shape[0] == len(texts)`** —— 不校验模型、不校验维度、不校验资产版本或 `content_hash`。

**v1_4 / v1_5 / v1_6 都是 260 张卡** → 在这三版之间切换时行数相同、缓存命中 → **用旧版本的向量给新卡片打分**，且无任何告警。

而 `./runtime:/app/runtime` 是持久化 volume（`compose.yaml:256`）→ 缓存跨重启留存，**风险真实存在**。

**修法**：缓存 key 或校验条件加入资产版本 + 模型标识 + 卡片内容哈希。

### Q17 `knowledge_degraded=True` 表示什么？不表示什么？

**表示：LLM provider 降级。**
```python
# consult_agent.py:1538 / 1638 / 1771
knowledge_degraded="knowledge_consult" in state.degraded_services
```
`knowledge_consult` 只在**生成模型不可用**时追加（`:959`、`:967`）。

**不表示：RAG 证据不足。两者毫无关系。**

RAG 证据不足的真实表现是另外三条路径：

| # | 路径 | 位置 |
|---|---|---|
| 1 | `rag_evidence == []` → prompt 里就是空数组 `[]` | `retriever.py:257-258` |
| 2 | `rag_decision != sufficient` → prompt 追加兜底段："知识库本轮没有检索到足够相关的参考证据…使用你掌握的通用宠物健康知识正常回答…不得把'没有知识卡'等同于'无法回答'" | `knowledge_consult_client.py:243-250`（有单测 `test_v73_rag_fallback_tone.py:64-77`） |
| 3 | `insufficient` **且** reason 含 `vague_general_query` **且** 无证据 **且** 无图片 → `_apply_provisional_no_evidence_guard` 清空 `possible_explanations`、重写 summary、清空 `answer_text` | `consult_agent.py:1378`、`:1347-1398` |

> ⚠ 路径 3 **只认 `vague_general_query` 这一个 reason**；`low_relevance` / `ambiguous_elimination` **不触发**收口。

### Q18 多宠问诊怎么确定问哪只？"请问是哪一只"会进历史吗？

**选择优先级（4 级，`ConsultCommand._resolve_pet()`）**：

| 级 | 规则 |
|---|---|
| 1 | **显式 `pet_ref`**——宠物名称 或 从 0 开始的数组下标字符串 |
| 2 | 正文中**唯一出现**的宠物名称 |
| 3 | 正文中的"猫/狗/犬"与列表中**唯一匹配**的 `species` |
| 4 | 仍无法确定 → 兼容旧调用方，**回退到数组第一只** |

调用方在同物种多宠、或同时询问多只宠物时，应显式传 `pet_ref`。

**「请问是哪一只」不会进历史。** `_fixed_pet_ambiguous_response` 分支（`:870-879`）直接 `return resp`，**没有 `await self._save_turn(...)`**。对比急症分支（`:848`）有存历史。

**后果**（三个连锁问题）：
1. 该轮**不进 Redis 历史**
2. `follow_up_questions` **不进 `case_facts.asked_questions`** → 下一轮 `_thin_questions`（`completeness_checker.py:347-348`）去重时**不知道已经问过**
3. 下一轮 `history` 与物种继承（`_apply_inferred_species`，`followup_tracker.py:199-204`）**读不到**

**这是设计上的自相矛盾**：这条路径**恰恰就是要问用户"是哪一只"**，却不记录自己问过。

**同类问题的其他路径**（都不写历史）：
- 无幂等键的急症短路（`:390-399`）—— 连对话存档 `_archive_dialogue` 都跳过
- `_refuse`（`:1543-1568`）—— 同步函数，无法 `await`
- `_error` / `_service_unavailable`

### Q19 `state_machine.py` 能直接删吗？删了有什么后果？

**能删，运行时零影响。** 证据：

| 检查 | 结果 |
|---|---|
| 全仓 grep `AgentState`/`_ALLOWED`/`assert_transition` | 命中**仅在自身 + 2 处 docstring**（`agent/__init__.py:9,14`、`state.py:61`） |
| `consult_agent.py` 是否 import 它 | **否** |
| `tests/` 中引用 | **0 命中**（7 个测试文件、47 条用例） |

它 `:414` 声称"运行时校验：`Agent._execute()` 中每次状态转移前调用 `assert_transition`" → **与代码不符**。
`:62-66` 要求"新增状态时必须补状态机测试" → **不存在任何此类测试**。

**即使当文档看也已失真**：
- 未建模的实际分支：out-of-scope 短路（`:652`）、vision_failed→REVIEW（`:744`）、fast answer（`:866`）、pet_ambiguous（`:877`）、REFUSE（`:647`）、生成失败（`:965/978`）
- `COMPLETENESS_AND_RISK → FIXED_SAFE_ANSWER` 标注"急症+预算不足"，但 EMERGENCY 实际走 `_fixed_urgent_response()`（`:840-849`）
- `OUTPUT_MODERATION` 的 docstring 提到 `state.fixed_safe_answer`（`:368`）—— **`ConsultState` 中不存在该字段**

**建议：不要直接删，但必须改变它的地位。** 两个选项：
1. **真正接入**——在 `_execute()` 各阶段边界调用 `assert_transition` + 补状态机测试
2. **降级为文档**——移到 `docs/` 下作为状态图

保留在 `app/agent/` 里的问题是：**它会造成"有运行时校验"的错觉**，而这是最危险的错觉——你会以为非法状态转移会被拦下。

### Q20 `docs/RAG.md` 至少 3 处与代码不符？

至少 8 处：

| # | 文档 | 实际 |
|---|---|---|
| 1 | `:32`"生产启动当前强制要求 `RAG_MODE=off`" | **无任何强制**。`is_prod`（`config.py:234-236`）全仓零引用；`main.py:30-74` 启动校验完全没提 RAG；`compose.yaml:247` 默认 **`grounded`** |
| 2 | `:7`"`assets/rag/v1_4/`（运行时默认）"、`:23-24`"运行时默认仍为 v1_4" | 实际 **v1_8**；且切换机制已不存在（全仓无 `RAG_INDEX_PATH` 配置） |
| 3 | `:29`"shadow：检索和急症匹配只写日志，不影响回答和分诊" | **三处不受模式约束**：追问注入（`:764-772`）、prompt 的 rag_decision 分支（`knowledge_consult_client.py:244-256`）、provisional 无证据收口（`:1086`） |
| 4 | `:9-16` 表格 260 卡 / 116 来源 / 300 队列 | v1_8 是 319 / 131 / 359 |
| 5 | `:37`"311 条证据记录中有 309 条只定位到 Abstract" | v1_4 实测 368/366；v1_8 实测 433/412 —— **对不上任何版本** |
| 6 | `:38`"平均每张卡约 1.2 个来源" | v1_4 = 1.42；v1_8 = 1.36（低估） |
| 7 | `:40`"没有向量召回和 reranker" | 向量召回**有代码**（生产 compose 已启用）；reranker 确实没有 |
| 8 | `:41`"780 条种子问题" | v1_8 是 957 |

**文档中仍然准确的两处**（值得肯定）：
- `:42`"当前 API 响应没有完整的用户可见引用字段" ✅ ——`RagHit.source_ids` 算了但无消费者，`ConsultResponse` 无 citation 字段
- `:39`"每张卡只有 2 条 `user_phrases`" —— 对 v1_4 恰好成立（2.00）；v1_8 是 3.44

---

## 实战题 · "高风险病例返回 status=error"怎么定位？

**核心陷阱**：人们会先往 GPU / vLLM / 容量方向查，而**根因在 `consult_agent.py:958`**。

### 定位步骤

| 步 | 动作 | 目的 |
|---|---|---|
| 1 | 查日志 `stage_done` 事件中 `risk_assess` 的 `risk_level` | **先确认风险等级确实是 HIGH** ——否则前提不成立 |
| 2 | 查 `pipeline_end` 的 `status` 与 error code | 若是 error + `KNOWLEDGE_CONSULT_UNAVAILABLE` → 卡在生成阶段 |
| 3 | `grep -n "knowledge_consult" app/agent/consult_agent.py` | 定位 `degraded_services` 追加点 → `:959`、`:967` |
| 4 | 读 `:919-978` 整段 | 会看到**两条失败路径都 `return self._service_unavailable(...)`** |
| 5 | `grep -n "build_fixed_safe_answer" app/agent/consult_agent.py` | 确认调用点只有 `:914`、`:1064`、`:1299` —— **没有任何一处在生成失败路径上** |
| 6 | 确认 `_service_unavailable` 的返回值 | 读 `:1608-1638` —— `status=ERROR`、`knowledge_degraded=True` |
| 7 | **复现验证**：停掉 9B（或把 `KNOWLEDGE_API_BASE_URL` 指向不可达地址），发高风险请求（如"狗狗抽搐"） | 观察返回 `status=error` 而非急症模板 → 确认根因 |

### 根因

`consult_agent.py:958-978` 的失败分支**缺少 HIGH/EMERGENCY 的风险等级判断**，直接把本可以走固定安全模板的请求降级为 ERROR。

### 修法

```python
# 在两条 return self._service_unavailable(...) 之前插入
if state.risk_result and state.risk_result.level in (RiskLevel.HIGH, RiskLevel.EMERGENCY):
    state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
    # 不 return —— 继续走输出审核(:1118) → 组装(:1141)
```

### 为什么这个 bug 值得优先修

它让 Q7 的四级安全漏斗对**最高风险的病例**完全失效——因为根本走不到漏斗。这与项目声明的"回答优先、高风险不短路"原则直接矛盾，且会在模型容量饱和时集中暴露（正是最需要安全兜底的时刻）。

---

## 附 · 必须记住的 6 件事

1. **只有 `ConsultAgent._execute()` 是流程**，其余名字都是数据模型或单一职责工具。判断方法：grep 它在 `_execute()` 里的调用点 + 看它写不写 `ConsultState`。
2. **急症判定在最前面，且是纯规则零依赖**——这是"所有依赖全挂也能返回急症处置"的保证。
3. **安全漏斗是四级**：本地修复 → 重写一次 → 再本地修复 → 固定模板。**但生成失败的 ERROR 路径绕过了它**（P0 缺陷）。
4. **`rag_mode` 生产是 `grounded`**，文档说的"强制 off"是错的。
5. **向量检索在生产跑 CPU**，且只对词面粗筛的 30 张候选精排——不是真正的向量召回。
6. **文档不可信，代码才是真相**。已知 28 处文档与代码不符。遇到任何设计问题，去看 `_execute()` 的调用顺序。
