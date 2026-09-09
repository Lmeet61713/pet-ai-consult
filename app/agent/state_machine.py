"""
问诊 Agent 的核心模块：状态机定义（v6.3 §9.3）

【核心定位】
本模块定义了 ConsultAgent 整个问诊处理流程的状态机。
它不是一个可运行的状态机引擎（如 state-machine-lib），而是**声明式定义**：
- 状态枚举（AgentState）：14 个状态的穷举
- 状态转移规则表（_ALLOWED）：每个状态允许迁移到的下一状态集合
- 校验函数（assert_transition）：用于测试状态流转合法性

【为什么不用状态机引擎？】
1. 生产级确定性：固定状态机比通用引擎更可预测，便于调试和监控
2. 性能：无额外抽象层，直接 dict 查找 O(1)
3. 可测试性：_ALLOWED 表可直接用于单元测试（覆盖所有合法/非法迁移）
4. 可维护性：新增状态时必须补状态机测试，防止遗漏

【设计哲学】
1. 固定状态机：不会自己拆解任务、自由调用工具的通用 Agent
   - 它的"智能"体现在：何时降级、何时追问、何时走固定模板
   - 所有状态转移都是预定义的，没有运行时动态决策

2. 绝对 deadline：所有阶段/重试共享同一个预算，重试不能重新获得完整预算
   - 这是防止"重试风暴"和"无限挂起"的关键

3. 安全兜底的"漏斗"逻辑：
   大模型生成 → 医疗规则审核 → 本地确定性修复 → 模型重写一次 → 固定安全模板
       ↑失败          ↑仍失败           ↑仍失败

【主流水线（按顺序执行）】
RECEIVED → TEXT_EMERGENCY_PRECHECK → LOAD_CONTEXT → INPUT_MODERATION →
IMAGE_ANALYSIS → COMPLETENESS_AND_RISK → (NORMAL|PROVISIONAL|URGENT)_GENERATION
→ MEDICAL_REVIEW → (REWRITE_ONCE) → FIXED_SAFE_ANSWER → OUTPUT_MODERATION → DONE

【分支说明】
- 任何阶段可终止到 DONE（拒绝/错误）
- 急症 + 预算不足 → FIXED_SAFE_ANSWER（固定急症模板）
- 医疗审核不通过 → REWRITE_ONCE → 重新 MEDICAL_REVIEW
- 图片分析失败 → 降级后继续 COMPLETENESS_AND_RISK

【状态流转示例】
正常流程：
RECEIVED → TEXT_EMERGENCY_PRECHECK → LOAD_CONTEXT → INPUT_MODERATION →
IMAGE_ANALYSIS → COMPLETENESS_AND_RISK → NORMAL_GENERATION → MEDICAL_REVIEW →
OUTPUT_MODERATION → DONE

急症流程：
RECEIVED → TEXT_EMERGENCY_PRECHECK → LOAD_CONTEXT → INPUT_MODERATION →
IMAGE_ANALYSIS → COMPLETENESS_AND_RISK → URGENT_GENERATION → MEDICAL_REVIEW →
OUTPUT_MODERATION → DONE

信息不足流程：
RECEIVED → TEXT_EMERGENCY_PRECHECK → LOAD_CONTEXT → INPUT_MODERATION →
IMAGE_ANALYSIS → COMPLETENESS_AND_RISK → PROVISIONAL_GENERATION → MEDICAL_REVIEW →
OUTPUT_MODERATION → DONE

医疗审核不通过流程：
... → MEDICAL_REVIEW → REWRITE_ONCE → MEDICAL_REVIEW → OUTPUT_MODERATION → DONE

急症 + 预算不足流程：
... → COMPLETENESS_AND_RISK → FIXED_SAFE_ANSWER → OUTPUT_MODERATION → DONE

【新增状态时必须补状态机测试】
测试用例应覆盖：
1. 所有合法迁移（从 _ALLOWED 表生成）
2. 所有非法迁移（断言 AssertionError）
3. 特殊规则：任何状态可终止到 DONE
"""
from __future__ import annotations

from enum import StrEnum


class AgentState(StrEnum):
    """问诊 Agent 处理的各个阶段状态枚举（14 个状态）。

    【状态分类】
    1. 起始状态：RECEIVED
    2. 预处理状态：TEXT_EMERGENCY_PRECHECK, LOAD_CONTEXT
    3. 安全审核状态：INPUT_MODERATION, IMAGE_ANALYSIS
    4. 评估状态：COMPLETENESS_AND_RISK
    5. 生成状态：NORMAL_GENERATION, PROVISIONAL_GENERATION, URGENT_GENERATION
    6. 医疗审核状态：MEDICAL_REVIEW, REWRITE_ONCE, FIXED_SAFE_ANSWER
    7. 输出审核状态：OUTPUT_MODERATION
    8. 终止状态：DONE

    【状态详解】
    """

    # ── 起始状态 ──
    RECEIVED = "received"
    """请求已接收，开始处理。
    
    触发时机：Agent.run() / Agent.run_stream() 被调用
    前置条件：ConsultCommand 已构建完成
    下一状态：TEXT_EMERGENCY_PRECHECK（唯一）
    
    处理内容：
    - 初始化 ConsultState.from_command(command)
    - 推断物种（_apply_inferred_species）
    - 创建 deadline 预算
    - 急症影子匹配（RAG 急症，可选）
    """

    # ── 预处理状态 ──
    TEXT_EMERGENCY_PRECHECK = "text_emergency_precheck"
    """文本急症预判（纯规则，最前方，零外部依赖）。
    
    触发时机：RECEIVED 完成后
    前置条件：state.text 已填充
    下一状态：LOAD_CONTEXT（唯一）
    
    处理内容：
    - emergency_rules.precheck_text(text, pet_info)
    - 命中 EMERGENCY 且无幂等键 → 直接固定急症模板（_fixed_urgent_response）
    - 命中急症后，任何失败都走固定急症模板
    
    设计要点：
    - 纯规则判定，不调外部服务（零外部依赖）
    - 最前置执行（在任何模型调用之前）
    - 命中后优先返回固定急症模板（不阻塞）
    """

    LOAD_CONTEXT = "load_context"
    """加载会话上下文（历史、摘要）。
    
    触发时机：TEXT_EMERGENCY_PRECHECK 完成后
    前置条件：state.text_emergency_precheck 已填充
    下一状态：INPUT_MODERATION（正常）/ DONE（审核不通过 → refuse）
    
    处理内容：
    - conversation_service.load_context(key) 从 Redis 载入历史
    - state.history = snapshot.turns（最近 20 轮）
    - state.history_summary = snapshot.summary（长历史压缩）
    - Redis 不可用 → degraded_services.append("redis")，单轮继续
    
    设计要点：
    - Redis 失败不阻断问诊（降级为单轮）
    - 历史用于物种推断和多轮上下文构建
    """

    # ── 安全审核状态 ──
    INPUT_MODERATION = "input_moderation"
    """输入内容安全审核（规则 + Guard 模型双通道）。
    
    触发时机：LOAD_CONTEXT 完成后
    前置条件：state.history 已加载
    下一状态：IMAGE_ANALYSIS（正常）/ DONE（审核不通过 → refuse/review）
    
    处理内容：
    - moderation.check_input(text, timeout_seconds, request_id)
    - 规则层：InputModerator.check（命中规则即 Unsafe）
    - 模型层：Guard（guard_enforced 才同步调用）
    - 医疗求助的 Violent 不拒（医疗豁免）
    - Guard 失败/解析失败 → 保守 Review
    
    分支处理：
    - parse_ok is False → 急症则固定模板，否则 _review
    - should_refuse_medical_request → _refuse（REFUSE + risk_flags）
    - _is_out_of_scope_query → 固定"非问诊范围"模板短路
    
    设计要点：
    - 场景化审核（医疗豁免）
    - 双通道（规则 + 模型）
    - 不知道 = 不通过（parse_ok=False → review）
    """

    IMAGE_ANALYSIS = "image_analysis"
    """图片视觉分析（VisionGateway + Qwen3.5-4B）。
    
    触发时机：INPUT_MODERATION 完成后
    前置条件：state.input_moderation 已通过
    下一状态：COMPLETENESS_AND_RISK（正常）/ DONE（图片全失败且无文字）
    
    处理内容：
    - image_service.analyze(image_inputs, text_hint, deadline.child(cap=15s))
    - VisionGateway 有界并发队列（VISION_CONCURRENCY=4，上限 12，45s）
    - vLLM Qwen3.5-4B（guided JSON schema）
    - Redis 结果缓存（key=sha256+model+prompt 版本 + 尺寸+tokens+text_hash）
    
    降级矩阵：
    - VisionTimeout/VisionUnavailable/VisionOutputInvalid → degraded_services+=vision
    - RequestDeadlineExceeded（15s 子预算耗尽）→ 若全局还有剩余则放弃图片继续
    - 图片全失败且无文字 → _review(reason="image_unavailable")
    
    后处理：
    - _detect_species_conflict（图文物种≠文字/档案 → 以文字为准 + 追问）
    - _detect_no_pet（无宠物：无文字→追问；有文字→忽略图片继续文字问诊）
    
    设计要点：
    - 独立 15s 子预算（deadline.child(cap=15s)）
    - 超时/失败降级为纯文本问诊，不中断
    - 图片永不存数据库（临时文件或对象存储）
    """

    # ── 评估状态 ──
    COMPLETENESS_AND_RISK = "completeness_and_risk"
    """信息完整性 + 风险评估（决定生成模式）。
    
    触发时机：IMAGE_ANALYSIS 完成后
    前置条件：state.vision_findings 已填充（可能为空）
    下一状态：NORMAL_GENERATION / PROVISIONAL_GENERATION / URGENT_GENERATION / 
              FIXED_SAFE_ANSWER（急症 + 预算不足）/ DONE（生成不可用且非急症）
    
    处理内容：
    1. RAG 检索（旁路，不阻断主链路）
       - rag_retriever.search(text, species)
       - 命中 SUFFICIENT 卡片 → 取卡片 questions_to_ask（≤3）给完整度检查用
    
    2. 完整度判断
       - completeness_checker.evaluate(state, rag_questions)
       - 多宠歧义 → pet_ambiguous（固定友好追问模板）
       - 无图无文 → hard_need（请上传/描述）
       - 图片不可用 → 补拍；眼病域 → 结构化追问缺失槽位
       - 短文本信息覆盖薄 → 先初步回答再追问（最多 2 个关键问题）
    
    3. 风险分级
       - emergency_rules.evaluate(text, red_flags, pet_info, precheck)
       - risk_engine.evaluate(state)
       - 图片质量 poor → 风险下限提到 MEDIUM + BOOK_VET
       - 眼部病例事实 → 逐条升档
    
    分支处理：
    - level==EMERGENCY → 固定急症模板（提前 _save_turn 后返回）
    - LOW/MEDIUM + 卡片可直答 + 非硬性信息缺失 → _fast_answer（卡片直答）
    - pet_ambiguous → 固定多宠追问模板
    - 否则进入生成（NORMAL/PROVISIONAL/URGENT）
    
    设计要点：
    - 完整度决定生成模式（normal/provisional）
    - 风险分级决定生成内容（urgent_guidance/normal）
    - 直答通道（不调生成模型，纯卡片渲染）
    """

    # ── 生成状态 ──
    NORMAL_GENERATION = "normal_generation"
    """正常模式生成问诊建议（信息充足）。
    
    触发时机：COMPLETENESS_AND_RISK 完成后，信息充足
    前置条件：state.completeness.need_more_info=False
    下一状态：MEDICAL_REVIEW（唯一）
    
    处理内容：
    - consultation_service.generate(state, deadline.child(cap=20.0))
    - KnowledgeConsultService → LocalOpenAIAdapter（vLLM Qwen3.5-9B）
    - response_format=json_schema（vLLM guided decoding 强约束 schema）
    - temperature=0.6, max_tokens 可配
    
    超时纪律：
    - 模型超时 = 容量饱和信号 → 不重试（防重试风暴）
    - 鉴权/连接类瞬时错误 → deadline 内重试一次
    
    设计要点：
    - 信息充足时正常生成
    - 结构化输出（json_schema 强约束）
    - 超时不重试（防重试风暴）
    """

    PROVISIONAL_GENERATION = "provisional_generation"
    """降级模式生成（信息不足，需追问）。
    
    触发时机：COMPLETENESS_AND_RISK 完成后，信息不足
    前置条件：state.completeness.need_more_info=True 且 reason in ("hard_need", "keyword_thin")
    下一状态：MEDICAL_REVIEW（唯一）
    
    处理内容：
    - consultation_service.generate_provisional(state, deadline.child(cap=20.0))
    - 简短初步建议 + 关键追问（最多 2 个关键问题）
    - 模型措辞审慎 + 就医边界
    
    设计要点：
    - 信息不足时不终止，给初步建议 + 追问
    - 回答优先：不因为信息不足就拒绝回答
    - 追问问题来自完整度检查（completeness.questions）
    """

    URGENT_GENERATION = "urgent_generation"
    """急症模式生成（高风险，固定模板）。
    
    触发时机：COMPLETENESS_AND_RISK 完成后，高风险
    前置条件：state.risk_result.level == RiskLevel.HIGH
    下一状态：MEDICAL_REVIEW（唯一）
    
    处理内容：
    - consultation_service.generate_urgent_guidance(state, deadline.child(cap=20.0))
    - 固定急症模板（"六要素"：原因、立即行动、禁止、就医紧急程度、风险等级、免责声明）
    - 20s 子预算
    
    设计要点：
    - 高风险不短路，仍走生成（但用固定模板）
    - 急症指导优先于正常生成
    - 固定模板保证安全性
    """

    # ── 医疗审核状态 ──
    MEDICAL_REVIEW = "medical_review"
    """医疗安全审核（四级漏斗）。
    
    触发时机：任何生成状态完成后
    前置条件：state.generated 已填充
    下一状态：REWRITE_ONCE（不通过）/ OUTPUT_MODERATION（通过）
    
    处理内容：
    - medical_safety_service.review(generated)
    - 四级漏斗：
      1. 药品安全违规（medication_blocklist.yaml）
      2. 确诊式断言违规（diagnosis_rules.py）
      3. 眼部不安全家庭操作
      4. 就医建议与风险等级/紧急度是否匹配
    
    不通过处理：
    - repair_locally（只修字段：软化断言/补免责/删冲突/眼部条目/上调风险与紧急度）
    - 复查 → 仍不通过且 deadline 有剩余 → rewrite_once
    - 重写仍失败 → build_fixed_safe_answer（兜底模板）
    
    后处理：
    - _apply_provisional_no_evidence_guard（无证据模糊主诉时清空 possible_explanations）
    - clean_owner_facing_language（英文术语→中文、伪科学清理、跨列表去重）
    
    设计要点：
    - 四级漏斗逐步收紧
    - 本地修复优先于重写（节省时间）
    - 重写仅一次（防无限循环）
    - 固定安全模板兜底
    """

    REWRITE_ONCE = "rewrite_once"
    """安全重写（医疗审核不通过时触发，仅一次）。
    
    触发时机：MEDICAL_REVIEW 不通过且 deadline 有剩余
    前置条件：state.medical_review.passed=False
    下一状态：MEDICAL_REVIEW（重新审核，唯一）
    
    处理内容：
    - rewrite_once(state, generated, violations)
    - 携带违规清单，仅修违规保留其余
    - 模型重新生成（带 rewrite_violations + rewrite_source）
    
    设计要点：
    - 仅重写一次（防无限循环）
    - 携带违规清单（模型知道要修什么）
    - 保留其余内容（不是完全重新生成）
    - 受 deadline 约束（预算不足则走固定模板）
    """

    FIXED_SAFE_ANSWER = "fixed_safe_answer"
    """固定安全模板回答（急症且预算不足，或重写仍失败）。
    
    触发时机：COMPLETENESS_AND_RISK 急症 + 预算不足，或 MEDICAL_REVIEW 重写仍失败
    前置条件：state.risk_result.level==EMERGENCY 或 medical_review 重写仍失败
    下一状态：OUTPUT_MODERATION（唯一）
    
    处理内容：
    - build_fixed_safe_answer(state)
    - 急症模板（EMERGENCY→升级为急症模板）
    - 固定"六要素"：原因、立即行动、禁止、就医紧急程度、风险等级、免责声明
    
    设计要点：
    - 绝对安全（不依赖模型）
    - 急症优先（即使预算不足也要给急症指导）
    - 兜底策略（所有失败都收敛到这里）
    """

    # ── 输出审核状态 ──
    OUTPUT_MODERATION = "output_moderation"
    """输出内容安全审核（规则 + Guard 模型双通道）。
    
    触发时机：MEDICAL_REVIEW 通过或 FIXED_SAFE_ANSWER 完成后
    前置条件：state.generated 或 state.fixed_safe_answer 已填充
    下一状态：DONE（审核通过→完成，不通过→review）
    
    处理内容：
    - moderation.check_output(answer, request_id)
    - 规则层：OutputModerator.check
    - 模型层：Guard（pet_consult_output 场景）
    - blocked → _review（REVIEW 状态）
    
    设计要点：
    - 最后一道安全闸
    - 双通道（规则 + 模型）
    - blocked 走 review（不静默放行）
    """

    # ── 终止状态 ──
    DONE = "done"
    """处理完成（终止状态，无后续）。
    
    触发时机：任何状态可终止到 DONE
    前置条件：无（任何状态都可跳转到 DONE）
    下一状态：无（终止状态）
    
    进入 DONE 的场景：
    - 正常完成：OUTPUT_MODERATION → DONE
    - 拒绝：INPUT_MODERATION 不通过 → DONE/REFUSE
    - 错误：生成不可用且非急症 → DONE/ERROR
    - 审核不通过：LOAD_CONTEXT → DONE（refuse）
    
    设计要点：
    - 任何状态都可终止到 DONE（安全阀）
    - DONE 是最终状态，无后续迁移
    - DONE 时 state.status 已确定（SUCCESS/REFUSE/REVIEW/ERROR）
    """


# ============================================================================
# 状态转移规则表（允许迁移表）
# ============================================================================
# 这是一个声明式的状态转移规则表，定义了每个状态允许迁移到的下一状态集合。
# 设计要点：
# 1. 使用 dict[AgentState, set[AgentState]] 结构，查找 O(1)
# 2. 每个状态的允许迁移集合是穷举的，不允许隐式迁移
# 3. 特殊规则：任何状态都可终止到 DONE（在 assert_transition 中处理）
#
# 使用场景：
# - 运行时校验：Agent._execute() 中每次状态转移前调用 assert_transition
# - 单元测试：遍历所有合法/非法迁移，确保状态机正确性
# - 文档生成：从 _ALLOWED 表自动生成状态流转图
# ============================================================================
_ALLOWED: dict[AgentState, set[AgentState]] = {
    # 起始状态 → 急症预判（唯一）
    AgentState.RECEIVED: {AgentState.TEXT_EMERGENCY_PRECHECK},

    # 急症预判 → 加载上下文（唯一）
    AgentState.TEXT_EMERGENCY_PRECHECK: {AgentState.LOAD_CONTEXT},

    # 加载上下文 → 输入审核（正常）/ DONE（审核不通过 → refuse）
    AgentState.LOAD_CONTEXT: {AgentState.INPUT_MODERATION, AgentState.DONE},

    # 输入审核 → 图片分析（正常）/ DONE（审核不通过 → refuse/review）
    AgentState.INPUT_MODERATION: {AgentState.IMAGE_ANALYSIS, AgentState.DONE},

    # 图片分析 → 完整度 + 风险（正常）/ DONE（图片全失败且无文字）
    AgentState.IMAGE_ANALYSIS: {AgentState.COMPLETENESS_AND_RISK, AgentState.DONE},

    # 完整度 + 风险 → 三种生成模式 / 固定安全模板 / DONE
    AgentState.COMPLETENESS_AND_RISK: {
        AgentState.NORMAL_GENERATION,         # 信息充足 → 正常生成
        AgentState.PROVISIONAL_GENERATION,    # 信息不足 → 降级生成
        AgentState.URGENT_GENERATION,         # 高风险 → 急症生成
        AgentState.FIXED_SAFE_ANSWER,         # 急症 + 预算不足 → 固定急症模板
        AgentState.DONE,                      # 生成不可用且非急症 → error
    },

    # 三种生成模式 → 医疗审核（唯一）
    AgentState.NORMAL_GENERATION: {AgentState.MEDICAL_REVIEW},
    AgentState.PROVISIONAL_GENERATION: {AgentState.MEDICAL_REVIEW},
    AgentState.URGENT_GENERATION: {AgentState.MEDICAL_REVIEW},

    # 医疗审核 → 重写一次（不通过）/ 输出审核（通过）
    AgentState.MEDICAL_REVIEW: {AgentState.REWRITE_ONCE, AgentState.OUTPUT_MODERATION},

    # 重写一次 → 重新医疗审核（唯一）
    AgentState.REWRITE_ONCE: {AgentState.MEDICAL_REVIEW},

    # 固定安全模板 → 输出审核（唯一）
    AgentState.FIXED_SAFE_ANSWER: {AgentState.OUTPUT_MODERATION},

    # 输出审核 → DONE（审核通过→完成，不通过→review）
    AgentState.OUTPUT_MODERATION: {AgentState.DONE},

    # 终止状态，无后续
    AgentState.DONE: set(),
}


def assert_transition(frm: AgentState, to: AgentState) -> None:
    """校验状态流转是否合法；非法则抛 AssertionError（状态机测试用）。

    【校验规则】
    1. 特殊规则：任何状态都可终止到 DONE（安全阀）
       - 这是为了防止某个阶段失败时无法终止
       - 例如：INPUT_MODERATION 不通过 → DONE/REFUSE

    2. 正常规则：to 必须在 frm 的允许迁移集合中
       - 从 _ALLOWED 表中查找
       - 非法迁移抛 AssertionError

    【使用场景】
    1. 运行时校验：Agent._execute() 中每次状态转移前调用
       ```python
       assert_transition(current_state, next_state)
       ```

    2. 单元测试：遍历所有合法/非法迁移
       ```python
       # 合法迁移
       for frm, allowed_tos in _ALLOWED.items():
           for to in allowed_tos:
               assert_transition(frm, to)  # 不应抛异常

       # 非法迁移
       for frm in AgentState:
           for to in AgentState:
               if to not in _ALLOWED[frm] and to != AgentState.DONE:
                   try:
                       assert_transition(frm, to)
                       assert False, "应抛 AssertionError"
                   except AssertionError:
                       pass  # 预期行为
       ```

    3. 文档生成：从 _ALLOWED 表自动生成状态流转图
       ```python
       for frm, allowed_tos in _ALLOWED.items():
           print(f"{frm.value} → {', '.join(t.value for t in allowed_tos)}")
       ```

    【参数说明】
    :param frm: 当前状态
    :param to: 目标状态
    :raises AssertionError: 如果状态流转非法

    【示例】
    ```python
    # 合法迁移
    assert_transition(AgentState.RECEIVED, AgentState.TEXT_EMERGENCY_PRECHECK)  # OK
    assert_transition(AgentState.INPUT_MODERATION, AgentState.DONE)  # OK（特殊规则）

    # 非法迁移
    assert_transition(AgentState.RECEIVED, AgentState.DONE)  # OK（特殊规则）
    assert_transition(AgentState.RECEIVED, AgentState.LOAD_CONTEXT)  # AssertionError!
    ```
    """
    # 特殊规则：任何状态都可终止到 DONE（安全阀）
    if to == AgentState.DONE:
        return  # 任何状态都可终止到 DONE

    # 正常规则：to 必须在 frm 的允许迁移集合中
    assert to in _ALLOWED[frm], f"非法状态流转: {frm} -> {to}"