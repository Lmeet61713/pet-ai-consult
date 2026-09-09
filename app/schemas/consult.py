"""问诊请求 / 响应 / 生成结果（v6.3 §7.4 / §7.5 / §14.2）

本模块定义了 ConsultAgent（问诊 Agent）的五层分层协议中的三层：
- ConsultCommand：第三层，运行入口参数，由 HTTP 路由层从 multipart 表单解析而来
- GeneratedConsultation：模型生成产物，包含大模型输出的结构化问诊建议
- ConsultResponse：第五层，对外响应，在 GeneratedConsultation 基础上补充状态/错误等字段
- KnowledgeConsultRequest：喂给大模型的统一内部请求，将 ConsultState 投影为模型可理解的格式

数据流转路径：
HTTP multipart 表单 → ConsultCommand（路由层解析） → ConsultState（Agent 内部状态总线）
→ 各阶段处理 → GeneratedConsultation（模型生成） → ConsultResponse（HTTP 响应）

设计要点：
1. ConsultCommand 是 Agent 的唯一输入契约，包含完整的问诊上下文
2. 多宠支持（2026-08-19）：pets 列表 + pet_ref 指定本次问诊对象
3. 幂等性通过 idempotency_key 保证，防止网络重试导致重复问诊
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.core.constants import (
    AnswerMode,
    ConsultStatus,
    DEFAULT_DISCLAIMER,
    RiskLevel,
    VetUrgency,
)
from app.schemas.auth import AuthContext        # 认证信息（tenant_id / user_id / JWT scope）
from app.schemas.common import ErrorDetail        # 错误详情（code / message / retryable）
from app.schemas.image import ProcessedImage, VisionFinding        # 图像处理结果（图片预处理 + 视觉分析发现）
from app.schemas.pet import PetInfo        # 宠物信息（name / species / breed / age / sex 等）


# ============================================================================
# 第三层：运行入口参数 - ConsultCommand
# ============================================================================
# 这是 Agent.run() / Agent.run_stream() 的输入参数，由 HTTP 路由层的 _prepare_command()
# 函数从 FastAPI 的 multipart 表单解析而来。它是 API 层与 Agent 层之间的协议边界。
#
# 字段分层：
# - 请求标识：request_id（全链路追踪）
# - 认证信息：auth（tenant_id / user_id，用于 Redis 会话键和权限校验）
# - 会话上下文：conversation_id（多轮问诊的会话标识）
# - 问诊内容：text + images（二者至少其一）
# - 宠物信息：pets（完整列表）+ pet_ref（本次指向）+ pet_info（解析后的当前宠物）
# - 控制参数：idempotency_key（防重复）+ total_timeout_seconds（超时覆盖）
# ============================================================================
class ConsultCommand(BaseModel):
    """agent.run() 的输入（路由层协议转换后）

    这是 ConsultAgent 的唯一输入契约，包含一次完整问诊所需的全部上下文信息。

    【多宠支持设计（2026-08-19）】
    - pets: 完整宠物列表（多只），例如用户档案中有"小白（猫）"和"大黄（狗）"
    - pet_ref: 本次问诊指定的宠物，可以是宠物名称（如"小白"）或数组下标字符串（如"0"）
    - pet_info: 兼容旧字段 = 解析出的"当前宠物"（pets[active] 或单只），供下游服务直接使用

    【字段校验规则】
    - text 和 images 至少有一个非空（由路由层校验，此处不强制）
    - images 最多 3 张、单张 ≤5MB（由 process_image 预处理保证）
    - conversation_id 格式：[A-Za-z0-9_-]{1,128}

    【使用示例】
    ```python
    # 路由层从 multipart 表单构建
    command = ConsultCommand(
        request_id="req_abc123",
        auth=AuthContext(tenant_id="t1", user_id="u1"),
        conversation_id="conv_001",
        text="我家猫咪一直呕吐怎么办？",
        images=[processed_image_1, processed_image_2],
        pets=[pet_cat, pet_dog],
        pet_ref="小白",  # 指定本次问诊对象是"小白"
    )
    # Agent 内部通过 _resolve_pet() 自动解析出 pet_info = pet_cat
    ```
    """

    # ── 请求标识 ──
    # 全链路唯一 ID，由中间件注入 request.state.request_id
    # 贯穿日志、Redis 键、RocketMQ 消息、SSE 事件、遥测数据
    request_id: str

    # ── 认证信息 ──
    # 包含 tenant_id（租户 ID）、user_id（用户 ID）、JWT scope（权限范围）
    # 用于构建 Redis 会话键：consult:{tenant_id}:{user_id}:{conversation_id}
    auth: AuthContext

    # ── 会话上下文 ──
    # 多轮问诊的会话标识，同一会话的历史轮次会从 Redis 载入
    # 格式约束：[A-Za-z0-9_-]{1,128}
    conversation_id: str

    # ── 问诊内容 ──
    # 用户描述的宠物症状/问题，≤4000 字符
    # 与 images 至少有一个非空（由路由层校验）
    text: str = ""

    # 预处理后的图片列表，最多 3 张
    # 每张图已通过 process_image() 完成：格式校验（JPEG/PNG/WEBP）、尺寸限制、SHA256 哈希计算
    images: list[ProcessedImage] = Field(default_factory=list)

    # ── 宠物信息 ──
    # 完整宠物列表（多宠场景），来自用户档案或本次请求传入
    # 例如：[PetInfo(name="小白", species="cat"), PetInfo(name="大黄", species="dog")]
    pets: list[PetInfo] = Field(default_factory=list)

    # 本次问诊指向的宠物标识，可选
    # 可以是宠物名称（如"小白"）或数组下标字符串（如"0"）
    # 不传时，Agent 会通过 _resolve_pet() 自动推断
    pet_ref: str | None = None

    # 解析后的"当前宠物信息"，由 _sync_active_pet() 自动填充
    # 下游服务（急症规则、风险评估、RAG 检索）直接使用此字段
    pet_info: PetInfo | None = None

    # ── 控制参数 ──
    # 幂等键，来自 Header `Idempotency-Key`，用于防止网络重试导致重复问诊
    # 指纹计算：sha256(conversation_id + text + pet_info + images.sha256)
    idempotency_key: str | None = None

    # 总超时时间（秒），可选覆盖默认值（默认 45s）
    # 用于 DeadlineFactory 创建绝对超时预算，所有阶段/重试共享此预算
    total_timeout_seconds: float | None = None

    @model_validator(mode="after")
    def _sync_active_pet(self) -> "ConsultCommand":
        """pet_info 与 pets 保持数据结构同步：pet_info = 当前活动宠物。

        这是 ConsultCommand 的核心校验逻辑，确保 pet_info 始终指向正确的宠物对象。

        【触发场景】
        1. 有 pets 列表但无 pet_info：通过 _resolve_pet() 自动解析
        2. 有 pet_info 但无 pets 列表：将 pet_info 包装为单元素列表

        【解析优先级】
        显式 pet_ref（名称或下标）> 文本中唯一出现的宠物名称 > 文本中唯一提到的物种且档案唯一匹配 > 回退第一只

        【设计意图】
        - 避免下游服务重复解析宠物信息
        - 保证 pet_info 始终与 pets 列表一致
        - 兼容旧版单宠请求（只有 pet_info 无 pets）
        """
        if self.pets and not self.pet_info:
            # 场景 1：有多宠列表但未指定当前宠物 → 自动解析
            active = self._resolve_pet(self.pets, self.pet_ref, self.text)
            self.pet_info = active
        elif self.pet_info and not self.pets:
            # 场景 2：旧版单宠请求（只有 pet_info）→ 包装为列表
            self.pets = [self.pet_info]
        return self

    @staticmethod
    def _resolve_pet(
        pets: list[PetInfo], pet_ref: str | None, text: str = ""
    ) -> PetInfo:
        """解析当前宠物：显式引用 > 问题中的名称/物种 > 默认第一只。

        【解析策略（四级优先级）】
        1. 显式 pet_ref：用户明确指定宠物名称或数组下标
           - 名称匹配：遍历 pets 列表，找到 name == pet_ref 的宠物
           - 下标匹配：pet_ref 是数字且 < len(pets)，返回 pets[int(pet_ref)]
           - 无法识别：回退第一只（保持旧接口兼容）

        2. 文本中唯一出现的宠物名称：
           - 遍历 pets 列表，检查 pet.name 是否出现在 text 中
           - 仅当唯一匹配时返回，避免歧义（如"小白和大黄都呕吐"）

        3. 文本中唯一提到的物种且档案唯一匹配：
           - 从 text 中提取物种词（"猫" → cat，"狗/犬" → dog）
           - 仅当物种唯一且 pets 中只有该物种的一只宠物时返回
           - 避免猜错对象（如两只狗或同时提到猫狗）

        4. 回退第一只：
           - 无法识别时返回 pets[0]
           - 保持旧接口兼容，不改变既有响应契约

        【参数说明】
        :param pets: 宠物信息列表，不能为空
        :param pet_ref: 显式指定的宠物标识，可选（名称或下标字符串）
        :param text: 问诊文本，用于自动识别宠物名称或物种
        :return: 解析后的当前宠物信息

        【示例】
        ```python
        # 场景 1：显式指定名称
        _resolve_pet([cat, dog], "小白", "小白一直呕吐") → cat

        # 场景 2：显式指定下标
        _resolve_pet([cat, dog], "1", "它一直呕吐") → dog

        # 场景 3：文本中出现唯一宠物名称
        _resolve_pet([cat, dog], None, "我家小白一直呕吐") → cat

        # 场景 4：文本中提到唯一物种
        _resolve_pet([cat], None, "我家猫一直呕吐") → cat

        # 场景 5：无法识别，回退第一只
        _resolve_pet([cat, dog], None, "它一直呕吐") → cat
        ```
        """
        if not pets:
            raise ValueError("pets 不能为空")

        # ── 优先级 1：显式 pet_ref ──
        if pet_ref:
            normalized_ref = pet_ref.strip()
            # 1.1 名称匹配：遍历 pets 列表，找到 name == pet_ref 的宠物
            for pet in pets:
                if pet.name and pet.name == normalized_ref:
                    return pet
            # 1.2 下标匹配：pet_ref 是数字且 < len(pets)
            if normalized_ref.isdigit() and int(normalized_ref) < len(pets):
                return pets[int(normalized_ref)]
            # 1.3 无法识别：回退第一只（保持旧接口兼容，不改变既有响应契约）
            return pets[0]

        # ── 优先级 2：文本中唯一出现的宠物名称 ──
        named_matches = [pet for pet in pets if pet.name and pet.name in text]
        if len(named_matches) == 1:
            return named_matches[0]

        # ── 优先级 3：文本中唯一提到的物种且档案唯一匹配 ──
        # 从 text 中提取物种词（"猫" → cat，"狗/犬" → dog）
        mentioned_species: set[str] = set()
        if "猫" in text:
            mentioned_species.add("cat")
        if "狗" in text or "犬" in text:
            mentioned_species.add("dog")

        # 仅当物种唯一时继续匹配（避免"猫和狗都呕吐"的歧义）
        if len(mentioned_species) == 1:
            target_species = next(iter(mentioned_species))  # 获取唯一物种
            species_matches = [
                pet
                for pet in pets
                if (pet.species or "").strip().lower() == target_species
            ]
            # 仅当该物种只有一只宠物时返回（避免两只狗的歧义）
            if len(species_matches) == 1:
                return species_matches[0]

        # ── 优先级 4：回退第一只 ──
        return pets[0]

# ============================================================================
# 就医建议 - VetRecommendation
# ============================================================================
# 独立的就医建议对象，与 ConsultStatus 相互独立。
# 设计意图：即使状态是 SUCCESS（问诊成功），也可能建议就医（recommended=True）。
# 就医建议由风险引擎（RiskEngine）和急症规则（EmergencyRules）综合判定。
# ============================================================================
class VetRecommendation(BaseModel):
    """独立就医建议（v6.3 §7.4：与 status 相互独立）

    【设计要点】
    - 与 ConsultStatus 正交：status=SUCCESS 时仍可能 recommended=True
    - urgency 分级：NONE < NON_URGENT < URGENT < EMERGENCY
    - reason 字段用于向用户解释为什么建议就医

    【使用场景】
    1. 低风险日常护理：recommended=False, urgency=NONE
    2. 中度症状观察：recommended=True, urgency=NON_URGENT（建议预约门诊）
    3. 高风险急症：recommended=True, urgency=EMERGENCY（立即就医）

    【示例】
    ```python
    VetRecommendation(
        recommended=True,
        urgency=VetUrgency.URGENT,
        reason="持续呕吐超过 24 小时，可能存在肠道梗阻风险"
    )
    ```
    """

    # 是否建议就医（由风险引擎综合判定）
    recommended: bool

    # 就医紧急程度（NONE / NON_URGENT / URGENT / EMERGENCY）
    urgency: VetUrgency

    # 建议就医的原因（向用户展示，需通俗易懂）
    reason: str = ""


# ============================================================================
# 模型生成产物 - GeneratedConsultation
# ============================================================================
# 这是大模型（Qwen3.5-9B）输出的结构化问诊建议，由 KnowledgeConsultService 生成。
#
# 重要设计约束：
# 1. possible_explanations 而非 diagnosis：防止业务层误当确诊
#    - 模型输出的是"可能的解释"，不是"诊断结果"
#    - 业务层不得据此开药或制定治疗方案
#
# 2. self_reported_confidence 只用于日志和评测：
#    - 不决定是否建议就医（由风险引擎独立判定）
#    - 不决定是否展示回答（即使 confidence 低也要展示）
#
# 3. answer_text 是两段式输出的正文段：
#    - 流式模式：直接转发给用户
#    - 非流式模式：为空时回退 _render() 从结构化字段拼装
# ============================================================================
class GeneratedConsultation(BaseModel):
    """KnowledgeConsult 结构化生成结果。

    这是大模型（Qwen3.5-9B）输出的问诊建议，包含完整的结构化字段。

    【重要设计约束】
    1. possible_explanations 而非 diagnosis（防止业务层误当确诊）
       - 模型输出的是"可能的解释"，不是"诊断结果"
       - 业务层不得据此开药或制定治疗方案

    2. self_reported_confidence 只用于日志和评测，不决定是否建议就医
       - 即使 confidence=0.3，也要展示回答（可能只是模型保守）
       - 就医建议由风险引擎独立判定，不受 confidence 影响

    3. answer_text 是两段式输出的正文段：
       - 流式模式：直接转发给用户（token_sink 逐 token 接收）
       - 非流式模式：为空时回退 _render() 从结构化字段拼装

    【字段分组】
    - 核心回答：summary / answer_text / possible_explanations
    - 行动指导：what_to_do_now / avoid_actions / what_to_monitor
    - 风险标识：risk_level / vet_recommendation / disclaimer
    - 交互元素：follow_up_questions / visible_findings
    - 元信息：answer_mode / self_reported_confidence

    【answer_mode 说明】
    - normal：正常生成（信息充足）
    - provisional：降级生成（信息不足，需追问）
    - urgent_guidance：急症指导（高风险，固定模板）
    """

    # 问诊摘要（简要总结用户问题）
    summary: str

    # 两段式输出的正文段（流式转发用户；非流式旧模式为空时回退 _render 渲染）
    # 流式模式：此字段包含完整的回答正文，通过 token_sink 逐 token 输出
    # 非流式模式：此字段为空，由 _render() 从下方结构化字段拼装
    answer_text: str = ""

    # 可见的检查结果（向用户展示的观察发现）
    # 例如："眼部有脓性分泌物"、"皮肤红肿"
    visible_findings: list[str] = Field(default_factory=list)

    # 可能的解释（注意：不是 diagnosis 确诊！）
    # 例如："可能是肠胃炎"、"可能是食物不耐受"
    # 业务层不得据此开药或制定治疗方案
    possible_explanations: list[str] = Field(default_factory=list)

    # 现在该做什么（即时行动指导）
    # 例如："暂时禁食 4-6 小时"、"提供清洁饮水"
    what_to_do_now: list[str] = Field(default_factory=list)

    # 应避免的行为（安全提示）
    # 例如："不要自行喂人用药物"、"不要强行催吐"
    avoid_actions: list[str] = Field(default_factory=list)

    # 需要监测的内容（后续观察指标）
    # 例如："体温变化"、"呕吐频率"、"精神状态"
    what_to_monitor: list[str] = Field(default_factory=list)

    # 继续追问的问题（信息不足时触发）
    # 例如："呕吐物是什么颜色？"、"最近是否换粮？"
    follow_up_questions: list[str] = Field(default_factory=list)

    # 风险等级（LOW / MEDIUM / HIGH / EMERGENCY）
    # 由模型根据症状综合评估，后续会被风险引擎修正
    risk_level: RiskLevel = RiskLevel.LOW

    # 回答模式（normal / provisional / urgent_guidance）
    # normal：信息充足，正常生成
    # provisional：信息不足，降级生成（需追问）
    # urgent_guidance：急症指导，固定模板
    answer_mode: AnswerMode = AnswerMode.NORMAL

    # 模型自我报告的置信度（0.0-1.0，仅用于日志和评测）
    # 不决定是否建议就医，不决定是否展示回答
    self_reported_confidence: float | None = Field(default=None, ge=0, le=1)

    # 就医建议（独立于 status，由风险引擎综合判定）
    vet_recommendation: VetRecommendation = Field(
        default_factory=lambda: VetRecommendation(recommended=False, urgency=VetUrgency.NONE)
    )

    # 免责声明（固定模板，必须包含）
    # 例如："本回答仅供参考，不能替代专业兽医诊断..."
    disclaimer: str = DEFAULT_DISCLAIMER


# ============================================================================
# 对外响应 - ConsultResponse
# ============================================================================
# 这是最终返回给 HTTP 客户端的响应对象，在 GeneratedConsultation 基础上补充：
# - 状态字段：status（SUCCESS / REFUSE / REVIEW / ERROR）
# - 错误信息：error（仅 status=ERROR 时存在）
# - 重试标识：retryable（是否可重试）
# - 图片发现：image_findings（VisionFinding 列表）
# - 风险标志：risk_flags（降级/警告信息）
#
# 校验规则（validate_by_status）：
# - status=SUCCESS 必须带 risk_level 和 disclaimer
# - status=SUCCESS 不能包含 error
# - status=ERROR 必须包含 error detail
# ============================================================================
class ConsultResponse(BaseModel):
    """知识库最终回复结果（HTTP 响应对象）。

    这是最终返回给 HTTP 客户端的响应对象，在 GeneratedConsultation 基础上补充状态/错误等字段。

    【数据流转】
    Agent._execute() → 生成 GeneratedConsultation → _answer() 组装 → ConsultResponse → HTTP 响应

    【status 状态说明】
    - SUCCESS：问诊成功，包含完整回答
    - REFUSE：输入审核不通过，拒绝回答（如违规内容）
    - REVIEW：需要人工审核（如 Guard 不可用时的保守策略）
    - ERROR：系统错误（如模型不可用、超时）

    【answer 字段来源】
    - 如果 generated.answer_text 非空：直接使用（流式模式）
    - 否则：由 _render() 从结构化字段拼装（summary + possible_explanations + what_to_do_now...）

    【校验规则（validate_by_status）】
    - status=SUCCESS 必须带 risk_level 和 disclaimer
    - status=SUCCESS 不能包含 error
    - status=ERROR 必须包含 error detail，且 retryable = error.retryable

    【使用示例】
    ```python
    # 成功响应
    ConsultResponse(
        request_id="req_123",
        conversation_id="conv_001",
        status=ConsultStatus.SUCCESS,
        answer="根据描述，可能是肠胃炎...",
        risk_level=RiskLevel.MEDIUM,
        disclaimer="本回答仅供参考...",
    )

    # 错误响应
    ConsultResponse(
        request_id="req_123",
        conversation_id="conv_001",
        status=ConsultStatus.ERROR,
        error=ErrorDetail(code="EXTERNAL_SERVICE_TIMEOUT", message="模型服务超时", retryable=True),
        retryable=True,
    )
    ```
    """

    # ── 请求标识 ──
    # 全链路唯一 ID，与 ConsultCommand.request_id 一致
    request_id: str

    # 会话 ID，与 ConsultCommand.conversation_id 一致
    conversation_id: str

    # 处理状态（SUCCESS / REFUSE / REVIEW / ERROR）
    status: ConsultStatus

    # ── 回答内容 ──
    # 回答模式（normal / provisional / urgent_guidance）
    answer_mode: AnswerMode | None = None

    # 回答正文（可能来自 answer_text 或 _render() 拼装）
    answer: str | None = None

    # 问诊摘要
    summary: str | None = None

    # 可能的解释（注意：不是 diagnosis 确诊！）
    possible_explanations: list[str] = Field(default_factory=list)

    # 现在该做什么（即时行动指导）
    what_to_do_now: list[str] = Field(default_factory=list)

    # 应避免的行为（安全提示）
    avoid_actions: list[str] = Field(default_factory=list)

    # 需要监测的内容（后续观察指标）
    what_to_monitor: list[str] = Field(default_factory=list)

    # ── 风险标识 ──
    # 风险等级（LOW / MEDIUM / HIGH / EMERGENCY）
    risk_level: RiskLevel | None = None

    # 风险标志（降级/警告信息，如 "vision_degraded"、"redis_unavailable"）
    risk_flags: list[str] = Field(default_factory=list)

    # 就医建议（独立于 status，由风险引擎综合判定）
    vet_recommendation: VetRecommendation | None = None

    # ── 图片发现 ──
    # 图片视觉分析结果（VisionFinding 列表）
    # 包含：image_quality / species_guess / observations / red_flags 等
    image_findings: list[VisionFinding] = Field(default_factory=list)

    # ── 交互元素 ──
    # 继续追问的问题（信息不足时触发）
    follow_up_questions: list[str] = Field(default_factory=list)

    # 模型自我报告的置信度（仅用于日志和评测）
    self_reported_confidence: float | None = None

    # 免责声明（必须包含）
    disclaimer: str | None = None

    # ── 错误信息 ──
    # 是否可重试（由 error.retryable 决定）
    retryable: bool = False

    # 错误详情（仅 status=ERROR 时存在）
    error: ErrorDetail | None = None

    # DeepSeek 调用异常并进入降级回答时置 true
    # 用于监控模型服务稳定性
    knowledge_degraded: bool = False

    # 预留：合作宠物医院推荐（暂不实现，接入时填充）
    hospital_recommendation: dict | None = None

    @model_validator(mode="after")
    def validate_by_status(self) -> "ConsultResponse":
        """根据 status 校验响应字段的合法性（v6.3 §7.5）。

        【校验规则】
        1. status=SUCCESS 必须带 risk_level 和 disclaimer
           - 这是医疗安全要求，任何成功响应都必须包含风险等级和免责声明

        2. status=SUCCESS 不能包含 error
           - 成功响应不应有错误信息（逻辑矛盾）

        3. status=ERROR 必须包含 error detail
           - 错误响应必须说明错误原因和是否可重试

        【设计意图】
        - 防止业务层收到不完整的响应
        - 保证医疗安全要求（risk_level + disclaimer）
        - 便于客户端根据 status 做不同处理
        """
        if self.status == ConsultStatus.SUCCESS:
            # 成功响应必须包含风险等级和免责声明（医疗安全要求）
            if self.risk_level is None or self.disclaimer is None:
                raise ValueError("success response requires risk_level and disclaimer")
            # 成功响应不应有错误信息
            if self.error is not None:
                raise ValueError("success response cannot include error")
        elif self.status == ConsultStatus.ERROR:
            # 错误响应必须包含错误详情
            if self.error is None:
                raise ValueError("error response requires error detail")
            # 重试标识由错误详情决定
            self.retryable = self.error.retryable
        return self


# ============================================================================
# 喂给大模型的统一内部请求 - KnowledgeConsultRequest
# ============================================================================
# 这是 ConsultAgent 发送给大模型（Qwen3.5-9B）的请求对象。
# 由 build_request(state, mode) 函数从 ConsultState 投影而来。
#
# 设计意图：
# 1. 业务层不直接依赖 Provider 原始字段（解耦）
# 2. 所有上下文都是 Agent 各安全/检索阶段"净化后"的产物
# 3. 模型看到的一切上下文都经过 state 字段传递
#
# 提示词模板结构（_render_user）：
# <user_input>当前问题</user_input>
# 宠物档案 / 全部宠物 / 本次问诊对象
# 图片摘要 {observations, red_flags, limitations}
# 对话摘要 / 最近对话
# 已确认病例事实 case_facts
# 风险上下文 {risk_level, matched_rules}
# 信息缺失项 / RAG 参考证据(受限参考) / RAG 决策
# 回答模式 normal|provisional|urgent_guidance
# [安全重写时附加] 上一版违规清单 rewrite_violations + rewrite_source
# ============================================================================
class KnowledgeConsultRequest(BaseModel):
    """统一内部请求（v6.3 §14.2：业务层不直接依赖 Provider 原始字段）。

    这是 ConsultAgent 发送给大模型（Qwen3.5-9B）的请求对象，由 build_request(state, mode)
    函数从 ConsultState 投影而来。

    【设计意图】
    1. 业务层不直接依赖 Provider 原始字段（解耦）
       - 模型接口可能切换（DeepSeek API / vLLM 本地），但此对象不变
       - 业务逻辑只依赖此对象，不依赖具体模型的字段

    2. 所有上下文都是 Agent 各安全/检索阶段"净化后"的产物
       - 图片发现经过 _detect_species_conflict / _detect_no_pet 过滤
       - 风险上下文经过 emergency_rules.evaluate + risk_engine.evaluate
       - RAG 证据经过白名单投影（仅 grounded 模式）

    3. 模型看到的一切上下文都经过 state 字段传递
       - 没有隐式全局变量
       - 没有重复解析请求

    【提示词模板结构（_render_user）】
    ```
    <user_input>当前问题</user_input>
    宠物档案 / 全部宠物 / 本次问诊对象
    图片摘要 {observations, red_flags, limitations}
    对话摘要 / 最近对话
    已确认病例事实 case_facts
    风险上下文 {risk_level, matched_rules}
    信息缺失项 / RAG 参考证据(受限参考) / RAG 决策
    回答模式 normal|provisional|urgent_guidance
    [安全重写时附加] 上一版违规清单 rewrite_violations + rewrite_source
    ```

    【字段分组】
    - 用户问题：user_question
    - 宠物上下文：pet_info / pets / active_pet_name
    - 多模态上下文：image_summary
    - 会话上下文：conversation_summary / recent_turns
    - 风险上下文：risk_context / case_facts / missing_information
    - RAG 上下文：rag_evidence / rag_decision
    - 控制参数：answer_mode / rewrite_violations / rewrite_source
    """

    # 用户问题（原始问诊文本）
    user_question: str

    # 当前宠物信息（字典格式，供模型理解）
    # 例如：{"name": "小白", "species": "cat", "age_months": 24, ...}
    pet_info: dict = Field(default_factory=dict)

    # 2026-08-19 多宠支持：完整宠物列表 + 当前问诊对象标识（供模型区分）
    # 例如：[{"name": "小白", "species": "cat"}, {"name": "大黄", "species": "dog"}]
    # 模型需要知道本次问诊指向哪只宠物（active_pet_name）
    pets: list[dict] = Field(default_factory=list)

    # 本次问诊指向的宠物名称（供模型在回答中明确对象）
    # 例如："小白"（模型会说"根据小白的症状..."而不是"根据宠物的症状..."）
    active_pet_name: str | None = None

    # 图片摘要（视觉分析结果的投影）
    # 结构：{observations: [...], red_flags: [...], limitations: [...]}
    # 注意：经过 _detect_species_conflict / _detect_no_pet 过滤后的结果
    image_summary: dict = Field(default_factory=dict)

    # 会话历史摘要（长历史压缩）
    # 当历史轮次过多时，由 conversation_service 压缩为摘要
    conversation_summary: str = ""

    # 最近对话轮次（最近 3-5 轮的原始文本）
    # 例如：["用户：它昨天开始呕吐", "助手：呕吐物是什么颜色？", ...]
    recent_turns: list[str] = Field(default_factory=list)

    # 风险上下文（急症规则判定结果）
    # 结构：{risk_level: "MEDIUM", matched_rules: ["rule_1", "rule_2"]}
    risk_context: dict = Field(default_factory=dict)

    # 已确认的病例事实（FollowUpTracker 抽取的结构化槽位）
    # 例如：{"eye_discharge": "purulent", "duration_hours": 48, ...}
    case_facts: dict = Field(default_factory=dict)

    # 缺失信息列表（完整度检查发现的需要追问的项）
    # 例如：["症状持续时间", "精神状态", "食欲变化"]
    missing_information: list[str] = Field(default_factory=list)

    # RAG 检索证据片段（仅在测试环境的 RAG grounded 模式填充）
    # 内容由本地卡片白名单投影生成，用于 grounded 回答
    # 例如：[{"card_id": "card_001", "content": "猫咪呕吐常见原因...", "score": 0.85}]
    rag_evidence: list[dict] = Field(default_factory=list)

    # RAG 决策状态（sufficient/insufficient/...）
    # 供 prompt 判断是否引导就医
    # sufficient：知识卡片足够回答，模型可参考卡片
    # insufficient：知识卡片不足，模型需用通用知识回答
    rag_decision: str = ""

    # 回答模式（normal / provisional / urgent_guidance）
    # 模型根据此模式调整回答风格：
    # - normal：完整回答
    # - provisional：简短初步建议 + 追问
    # - urgent_guidance：急症指导（固定模板）
    answer_mode: str = "normal"

    # V1.1 P1-2：安全重写时携带上一版违规清单（空 = 普通生成）
    # 例如：["确诊式断言", "药品安全违规"]
    # 模型需要修复这些违规，但保留其余内容
    rewrite_violations: list[str] = Field(default_factory=list)

    # 安全重写时的上一版生成结果（供模型参考）
    # 结构：GeneratedConsultation 的字典表示
    # 模型需要在此基础上修复违规，而不是重新生成
    rewrite_source: dict = Field(default_factory=dict)