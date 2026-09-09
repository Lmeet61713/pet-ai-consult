"""
ConsultState —— 单次问诊请求生命周期内的状态总线（v6.3 §7.6）

【核心定位】
ConsultState 是 ConsultAgent 内部状态机各阶段共享的"黑板"（状态总线）。
在请求处理过程中，各个阶段（输入审核、图片分析、风险评定、生成、医疗审核、输出审核）
的结果会逐步填充到该状态对象中，最终形成完整的问诊响应。

【数据流转路径】
HTTP multipart 表单 → ConsultCommand（路由层解析） → ConsultState.from_command()（初始化）
→ 各阶段处理（逐步回写 state 字段） → ConsultResponse（HTTP 响应）

【设计哲学】
1. 单一数据源：所有 Agent 阶段与服务共享同一个 ConsultState 引用
   - 没有隐式全局变量
   - 没有重复解析请求
   - 任何中间产物都通过 state 字段传递

2. 阶段回写模式：每个阶段只做两件事
   - ① 读 state 上游产物（如 vision_findings、history）
   - ② 把结果回写 state（如 input_moderation、risk_result）

3. 反向隔离：ConsultState.from_command() 从 ConsultCommand 提取核心字段
   - 后续所有服务只依赖 ConsultState，不依赖请求层（API 层解耦）

【字段分组（按处理流程顺序）】
1. 请求元信息（会话、用户、宠物）→ 从 ConsultCommand 初始化
2. 输入预处理（文本、图片、历史）→ 从 ConsultCommand 初始化 + Redis 载入
3. 急症预判与降级 → 纯规则预判 + 降级服务记录
4. 安全审核流水线 → 输入审核 → 图片分析 → 完整性/风险 → 生成 → 医疗审核 → 输出审核
5. RAG 检索结果 → 知识卡片检索 + grounded 证据
6. 最终状态与警告 → 处理状态 + 警告信息

【使用示例】
```python
# 1. 初始化（从 ConsultCommand）
state = ConsultState.from_command(command)

# 2. 各阶段回写（Agent._execute() 中）
state.text_emergency_precheck = self.emergency_rules.precheck_text(...)
state.history = snapshot.turns
state.input_moderation = await self.moderation.check_input(...)
state.vision_findings = await self.image_service.analyze(...)
state.rag_result = self.rag_retriever.search(...)
state.completeness = self.completeness_checker.evaluate(state)
state.risk_result = self.risk_engine.evaluate(state)
state.generated = await self.consultation_service.generate(state)
state.medical_review = await self.medical_safety_service.review(state)
state.output_moderation = await self.moderation.check_output(...)

# 3. 最终组装（Agent._answer() 中）
response = ConsultResponse(
    request_id=state.request_id,
    status=state.status,
    risk_level=state.risk_result.level,
    ...
)
```

【状态流转由 ConsultAgent 的状态机驱动，每个阶段产出写入对应字段】
状态机定义见 state_machine.py（14 个状态 + 允许迁移表）
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import ConsultStatus
from app.rag.models import RagResult
from app.schemas.conversation import ConversationTurn
from app.schemas.consult import ConsultCommand, GeneratedConsultation
from app.schemas.followup import CaseFacts
from app.schemas.image import ProcessedImage, VisionFinding
from app.schemas.pet import PetInfo
from app.schemas.safety import (
    CompletenessResult,
    EmergencyResult,
    MedicalReviewResult,
    ModerationResult,
    RiskResult,
)


class ConsultState(BaseModel):
    """单次请求生命周期内的状态总线（v6.3 §7.6）。

    这是 ConsultAgent 内部状态机各阶段共享的"黑板"，所有中间产物都通过 state 字段传递。

    【设计哲学】
    1. 单一数据源：所有 Agent 阶段与服务共享同一个 ConsultState 引用
       - 没有隐式全局变量
       - 没有重复解析请求
       - 任何中间产物都通过 state 字段传递

    2. 阶段回写模式：每个阶段只做两件事
       - ① 读 state 上游产物（如 vision_findings、history）
       - ② 把结果回写 state（如 input_moderation、risk_result）

    3. 反向隔离：ConsultState.from_command() 从 ConsultCommand 提取核心字段
       - 后续所有服务只依赖 ConsultState，不依赖请求层（API 层解耦）

    【字段分组（按处理流程顺序）】
    1. 请求元信息（会话、用户、宠物）→ 从 ConsultCommand 初始化
    2. 输入预处理（文本、图片、历史）→ 从 ConsultCommand 初始化 + Redis 载入
    3. 急症预判与降级 → 纯规则预判 + 降级服务记录
    4. 安全审核流水线 → 输入审核 → 图片分析 → 完整性/风险 → 生成 → 医疗审核 → 输出审核
    5. RAG 检索结果 → 知识卡片检索 + grounded 证据
    6. 最终状态与警告 → 处理状态 + 警告信息

    【使用示例】
    ```python
    # 1. 初始化（从 ConsultCommand）
    state = ConsultState.from_command(command)

    # 2. 各阶段回写（Agent._execute() 中）
    state.text_emergency_precheck = self.emergency_rules.precheck_text(...)
    state.history = snapshot.turns
    state.input_moderation = await self.moderation.check_input(...)
    state.vision_findings = await self.image_service.analyze(...)
    state.rag_result = self.rag_retriever.search(...)
    state.completeness = self.completeness_checker.evaluate(state)
    state.risk_result = self.risk_engine.evaluate(state)
    state.generated = await self.consultation_service.generate(state)
    state.medical_review = await self.medical_safety_service.review(state)
    state.output_moderation = await self.moderation.check_output(...)

    # 3. 最终组装（Agent._answer() 中）
    response = ConsultResponse(
        request_id=state.request_id,
        status=state.status,
        risk_level=state.risk_result.level,
        ...
    )
    ```
    """

    # ========================================================================
    # 第一组：请求元信息（从 ConsultCommand 初始化）
    # ========================================================================
    # 这些字段在 ConsultState.from_command() 时初始化，后续只读不写。
    # 它们是整个问诊流程的基础上下文，所有阶段都会用到。
    # ========================================================================

    # 全链路唯一请求 ID（贯穿日志、Redis 键、RocketMQ 消息、SSE 事件、遥测数据）
    # 由中间件注入 request.state.request_id，格式如 "req_abc123"
    request_id: str

    # 租户 ID（多租户隔离，用于 Redis 会话键和权限校验）
    # 例如："tenant_001"
    tenant_id: str

    # 用户 ID（宠物主账号，用于 Redis 会话键和权限校验）
    # 例如："user_12345"
    user_id: str

    # 会话 ID（多轮问诊的会话标识，同一会话的历史轮次会从 Redis 载入）
    # 格式约束：[A-Za-z0-9_-]{1,128}
    # 例如："conv_001"
    conversation_id: str

    # 用户问诊文本（原始输入，≤4000 字符）
    # 例如："我家猫咪一直呕吐怎么办？"
    text: str

    # 当前宠物信息（解析后的"本次问诊对象"）
    # 由 ConsultCommand._resolve_pet() 自动解析，下游服务直接使用
    # 例如：PetInfo(name="小白", species="cat", age_months=24, ...)
    pet_info: PetInfo | None = None

    # 2026-08-19 多宠支持：完整宠物列表（多宠场景）
    # 例如：[PetInfo(name="小白", species="cat"), PetInfo(name="大黄", species="dog")]
    # 用于多宠歧义检测和模型上下文构建
    pets: list[PetInfo] = Field(default_factory=list)

    # 本次问诊指向的宠物标识（可选，用户显式指定）
    # 可以是宠物名称（如"小白"）或数组下标字符串（如"0"）
    # 例如："小白" 或 "0"
    pet_ref: str | None = None

    # ========================================================================
    # 第二组：输入预处理（从 ConsultCommand 初始化 + Redis 载入）
    # ========================================================================
    # 这些字段在 ConsultState.from_command() 时部分初始化（image_inputs），
    # 部分在 LOAD_CONTEXT 阶段从 Redis 载入（history、history_summary）。
    # ========================================================================

    # 预处理后的图片列表（最多 3 张）
    # 每张图已通过 process_image() 完成：格式校验（JPEG/PNG/WEBP）、尺寸限制、SHA256 哈希计算
    # 例如：[ProcessedImage(image_id="img_1", filename="cat.jpg", format="JPEG", ...)]
    image_inputs: list[ProcessedImage] = Field(default_factory=list)

    # 会话历史（最近 20 轮的对话记录，从 Redis 载入）
    # 例如：[ConversationTurn(user_text="它昨天开始呕吐", assistant_answer="呕吐物是什么颜色？", ...)]
    # 用于多轮问诊上下文构建和物种推断
    history: list[ConversationTurn] = Field(default_factory=list)

    # 会话历史摘要（长历史时压缩，用于模型上下文）
    # 当历史轮次过多时，由 conversation_service 压缩为摘要
    # 例如："用户描述猫咪小白呕吐 2 天，精神不振，建议禁食观察..."
    history_summary: str | None = None

    # 当前病例事实（FollowUpTracker 抽取的结构化槽位）
    # 例如：CaseFacts(eye_discharge="purulent", duration_hours=48, ...)
    # 用于风险引擎评估和模型上下文构建
    case_facts: CaseFacts = Field(default_factory=CaseFacts)

    # ========================================================================
    # 第三组：急症预判与降级（流程最前，纯规则）
    # ========================================================================
    # 这些字段在 TEXT_EMERGENCY_PRECHECK 阶段初始化，是流程最前置的判定结果。
    # 急症预判是纯规则判定，不依赖任何外部服务（零外部依赖）。
    # ========================================================================

    # 文字急症预判结果（纯规则，最前置，零外部依赖）
    # 由 emergency_rules.precheck_text() 判定，例如命中"呼吸困难"、"大出血"等急症规则
    # 命中后任何失败都走固定急症模板（_fixed_urgent_response）
    # 例如：EmergencyResult(level=RiskLevel.EMERGENCY, force_urgent_guidance=True, ...)
    text_emergency_precheck: EmergencyResult | None = None

    # 已降级的服务列表（记录哪些服务不可用或超时）
    # 例如：["redis", "vision", "knowledge_consult"]
    # 降级不阻断问诊，但会影响回答质量（最终进入 risk_flags）
    degraded_services: list[str] = Field(default_factory=list)

    # ========================================================================
    # 第四组：安全审核流水线（按顺序逐步回写）
    # ========================================================================
    # 这是 Agent 状态机的核心处理链路，每个阶段的产物都会回写到对应字段。
    # 流水线顺序：输入审核 → 急症规则 → 图片分析 → 完整性/风险 → 生成 → 医疗审核 → 输出审核
    #
    # 设计要点：
    # 1. 每个阶段只读上游产物，回写自己的结果
    # 2. 任何阶段失败都有降级策略（不阻断问诊）
    # 3. 所有中间产物最终都会进入 ConsultResponse 或存档
    # ========================================================================

    # 输入内容审核结果（规则 + Guard 模型双通道）
    # 由 moderation.check_input() 判定，例如命中敏感词、违规内容等
    # 医疗求助的 Violent 不拒（医疗豁免），Guard 失败保守 review
    # 例如：ModerationResult(is_safe=True, should_refuse_medical_request=False, parse_ok=True, ...)
    input_moderation: ModerationResult | None = None

    # 急症规则判定结果（合并文字 + 图片 red_flags + 档案）
    # 由 emergency_rules.evaluate() 判定，综合文字预判和图片发现
    # 例如：EmergencyResult(level=RiskLevel.HIGH, matched_rules=["rule_1", "rule_2"], ...)
    emergency_result: EmergencyResult | None = None

    # 图片视觉分析发现（VisionFinding 列表，每张图一个发现）
    # 由 image_service.analyze() 通过 VisionGateway 调用 Qwen3.5-4B 生成
    # 包含：image_quality / species_guess / observations / red_flags / needs_more_images 等
    # 例如：[VisionFinding(image_quality="good", observations=["眼部有脓性分泌物"], red_flags=["conjunctivitis"], ...)]
    vision_findings: list[VisionFinding] = Field(default_factory=list)

    # 信息完整性评估结果（是否信息不足需要追问）
    # 由 completeness_checker.evaluate() 判定，决定 normal/provisional 生成模式
    # 例如：CompletenessResult(need_more_info=True, reason="hard_need", questions=["症状持续时间？"], ...)
    completeness: CompletenessResult | None = None

    # 综合风险评估结果（合并急症规则 + 图片质量 + 病例事实）
    # 由 risk_engine.evaluate() 判定，最终分级 LOW/MEDIUM/HIGH/EMERGENCY
    # 例如：RiskResult(level=RiskLevel.MEDIUM, vet_urgency=VetUrgency.NON_URGENT, reasons=["图片质量 poor"], ...)
    risk_result: RiskResult | None = None

    # 大模型生成的问诊建议（Qwen3.5-9B 输出）
    # 由 consultation_service.generate() / generate_provisional() / generate_urgent_guidance() 生成
    # 包含：summary / answer_text / possible_explanations / what_to_do_now / risk_level 等
    # 例如：GeneratedConsultation(summary="猫咪呕吐可能原因...", answer_text="根据描述...", ...)
    generated: GeneratedConsultation | None = None

    # 医疗安全审核结果（四级漏斗：药品安全 → 确诊断言 → 眼部操作 → 就医建议匹配）
    # 由 medical_safety_service.review() 判定，不通过时触发 repair_locally 或 rewrite_once
    # 例如：MedicalReviewResult(passed=False, violations=["确诊式断言", "药品安全违规"], ...)
    medical_review: MedicalReviewResult | None = None

    # 输出内容审核结果（规则 + Guard 模型双通道）
    # 由 moderation.check_output() 判定，blocked 时走 _review 流程
    # 例如：ModerationResult(is_safe=True, blocked=False, ...)
    output_moderation: ModerationResult | None = None

    # ========================================================================
    # 第五组：RAG 检索结果（旁路，不阻断主链路）
    # ========================================================================
    # RAG 检索是旁路处理，失败不影响主链路（不阻断问诊）。
    # 检索结果用于 grounded 回答和完整度检查（卡片追问查缺）。
    # ========================================================================

    # 知识库检索结果（词法 Shadow 检索或混合检索）
    # 由 rag_retriever.search() 检索，包含决策状态和命中卡片列表
    # 例如：RagResult(decision=RagDecisionStatus.SUFFICIENT, hits=[...], top_score=0.85, ...)
    rag_result: RagResult | None = None

    # 检索证据片段列表（仅在测试环境的 RAG grounded 模式填充）
    # 由 rag_retriever.build_grounded_evidence() 构建，内容由本地卡片白名单投影生成
    # 例如：[{"card_id": "card_001", "content": "猫咪呕吐常见原因...", "score": 0.85}]
    rag_evidence: list[dict] = Field(default_factory=list)

    # ========================================================================
    # 第六组：最终状态与警告（处理完成后填充）
    # ========================================================================
    # 这些字段在流程结束时填充，用于最终响应和存档。
    # ========================================================================

    # 最终处理状态（SUCCESS / REFUSE / REVIEW / ERROR）
    # 由 Agent._execute() 根据各阶段结果决定
    # 例如：ConsultStatus.SUCCESS（问诊成功）
    status: ConsultStatus | None = None

    # 处理过程中的警告信息（降级/冲突/异常等）
    # 例如：["vision_degraded", "species_conflict", "redis_unavailable"]
    # 最终会进入 ConsultResponse.risk_flags
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_command(cls, command: ConsultCommand) -> "ConsultState":
        """从 ConsultCommand 请求命令创建初始状态对象。

        【设计意图：反向隔离】
        ConsultCommand 包含 API 层解析后的完整请求参数（auth、idempotency_key 等），
        该方法仅提取出问诊处理所需的核心字段，避免后续流程直接依赖请求层。

        这样设计的好处：
        1. Agent 内部服务只依赖 ConsultState，不依赖 API 层数据结构
        2. 如果 API 层字段变化，只需修改 from_command()，不影响 Agent 内部逻辑
        3. 便于测试：可以直接构造 ConsultState，不需要构造完整的 ConsultCommand

        【字段映射】
        - command.request_id → state.request_id（全链路追踪）
        - command.auth.tenant_id → state.tenant_id（多租户隔离）
        - command.auth.user_id → state.user_id（用户标识）
        - command.conversation_id → state.conversation_id（会话标识）
        - command.text → state.text（问诊文本）
        - command.pet_info → state.pet_info（当前宠物）
        - command.pets → state.pets（完整宠物列表）
        - command.pet_ref → state.pet_ref（宠物引用）
        - command.images → state.image_inputs（预处理后的图片）

        【未映射字段】
        - command.idempotency_key：由 Agent 层单独处理（幂等检查）
        - command.auth.scope：由 API 层处理（权限校验）
        - command.total_timeout_seconds：由 deadline_factory 处理（超时预算）

        【使用示例】
        ```python
        # 路由层从 multipart 表单构建 ConsultCommand
        command = ConsultCommand(
            request_id="req_abc123",
            auth=AuthContext(tenant_id="t1", user_id="u1"),
            conversation_id="conv_001",
            text="我家猫咪一直呕吐怎么办？",
            images=[processed_image_1],
            pets=[pet_cat, pet_dog],
            pet_ref="小白",
        )

        # Agent 层从 ConsultCommand 创建 ConsultState
        state = ConsultState.from_command(command)
        # 此时 state 包含：request_id、tenant_id、user_id、conversation_id、
        # text、pet_info、pets、pet_ref、image_inputs
        # 其他字段（history、vision_findings、generated 等）为默认值
        ```
        """
        return cls(
            request_id=command.request_id,
            tenant_id=command.auth.tenant_id,
            user_id=command.auth.user_id,
            conversation_id=command.conversation_id,
            text=command.text,
            pet_info=command.pet_info,
            pets=command.pets,
            pet_ref=command.pet_ref,
            image_inputs=command.images,
        )