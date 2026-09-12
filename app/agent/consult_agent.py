"""ConsultAgent —— 问诊 Agent 主控制器（v6.3 §9）

【核心职责】
ConsultAgent 是整个问诊系统的"大脑"，负责：
1. 接收用户问诊请求（文字 + 图片）
2. 按固定状态机顺序执行 10+ 个处理阶段
3. 协调外部服务（Vision、RAG、Guard、生成模型）
4. 执行安全审核（输入审核、医疗审核、输出审核）
5. 返回结构化问诊结果（ConsultResponse）

【关键设计原则】
1. 文字急症预判最前置（纯规则，不调外部服务）
   - 命中急症后任何失败都走固定急症模板
   - 确保急症场景下不依赖模型可用性

2. 绝对 deadline 预算管理系统
   - 各阶段从统一预算领取时间，重试不得重新获得完整预算
   - 防止某个阶段超时导致整个请求挂起
   - 使用 DeadlineFactory 创建总预算，各阶段通过 child() 获取子预算

3. Vision 降级策略
   - Vision 超时/OOM/JSON 失败 → 记录 degraded，文字可继续
   - 图片失败不阻断问诊，只在回答中标注 provisional

4. 回答优先策略
   - 高风险不短路（切 urgent_guidance 模式）
   - 信息不足不终止（provisional 模式，给初步建议 + 追问）

5. 医疗安全漏斗
   - 医疗检查失败 → 受 deadline 限制重写一次 → 仍失败 → 固定安全回答模板
   - 确保回答始终符合医疗安全规范

【执行流程概览】
1. 文字急症预判 → 2. 加载历史 → 3. 输入审核 → 4. 图片解析 →
5. RAG 检索 → 6. 完整度判断 → 7. 风险分级 → 8. 生成回答 →
9. 医疗审核 → 10. 输出审核 → 11. 组装响应

【状态机集成】
ConsultAgent 使用 ConsultState 作为状态总线，所有阶段的中间结果都写入 state，
最终通过 _answer() 方法从 state 组装成 ConsultResponse。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from app.agent.completeness_checker import CompletenessChecker
from app.agent.state import ConsultState
from app.core.constants import (
    AnswerMode,
    ConsultStatus,
    DEFAULT_DISCLAIMER,
    RiskLevel,
    VetUrgency,
)
from app.core.config import Settings
from app.core.deadline import DeadlineFactory
from app.core.exceptions import (
    ConversationConflictError,
    ExternalServiceTimeout,
    KnowledgeConsultUnavailable,
    RedisUnavailable,
    RequestDeadlineExceeded,
    VisionOutputInvalid,
    VisionTimeout,
    VisionUnavailable,
)
from app.repositories.idempotency_repository import IdempotencyRepository
from app.rag.models import RagDecisionStatus
from app.rag.retriever import ShadowRetriever, normalize_species
from app.rag.emergency_shadow import V14EmergencyShadowMatcher
from app.safety.emergency_rules import EmergencyRuleEngine
from app.safety.input_moderator import InputModerator
from app.safety.output_moderator import OutputModerator
from app.schemas.consult import ConsultCommand, ConsultResponse, VetRecommendation
from app.schemas.common import ErrorDetail
from app.schemas.pet import PetInfo
from app.services.conversation_service import ConversationService
from app.services.consultation_service import ConsultationService
from app.services.dialogue_archive import DialogueArchive
from app.services.image_service import ImageService
from app.services.medical_safety_service import MedicalSafetyService
from app.services.moderation_service import ModerationService
from app.utils.time import utc_now_iso

logger = logging.getLogger(__name__)


# ============================================================
# 模块级工具函数
# ============================================================

def _should_retry_knowledge_failure(exc: KnowledgeConsultUnavailable) -> bool:
    """判断知识问诊失败是否应该重试。

    【设计哲学】
    客户端目前把连接/读取超时统一映射成 KnowledgeConsultUnavailable，并在消息中
    保留"超时"。高并发下立即重试会把已饱和模型的请求量瞬间翻倍，因此超时必须
    直接结束；鉴权、HTTP 状态或瞬时连接错误仍沿用原有的一次重试策略。

    【重试策略】
    - 超时类故障 → 不重试（模型已饱和，重试会加重负载）
    - 鉴权/HTTP 错误 → 重试一次（可能是瞬时故障）
    - 连接错误 → 重试一次（可能是瞬时网络问题）

    【使用场景】
    在 _execute() 的生成阶段捕获 KnowledgeConsultUnavailable 后调用，
    决定是否进入一次性重试逻辑。

    :param exc: KnowledgeConsultUnavailable 异常对象
    :return: True 表示应该重试，False 表示不应该重试
    """
    return "超时" not in str(exc)


# ============================================================
# 模块级常量定义
# ============================================================

# 非宠物问诊识别模式（高精度规则匹配，宁可少拦不误拦）
# 用于识别明确的天气/气温/下雨等与宠物无关的问题
_OUT_OF_SCOPE_PATTERNS = (
    re.compile(r"(?:今天|明天|后天|现在|当地)?(?:的)?天气(?:怎么样|如何|预报|情况)?"),
    re.compile(r"(?:今天|明天|后天|现在|当地)?(?:的)?气温(?:多少|怎么样|如何)?"),
    re.compile(r"(?:今天|明天|后天|现在|当地)?(?:会不会|是否|有)?下雨"),
)

# 宠物健康语境关键词（命中这些词说明是宠物相关问题，不应被拦）
# 包含：物种名称、症状描述、护理行为、医疗相关
_PET_CONTEXT_TERMS = (
    "猫", "狗", "犬", "宠物", "洗澡", "喂", "食欲", "精神", "呕吐", "吐",
    "腹泻", "拉稀", "便", "尿", "皮肤", "掉毛", "伤口", "腿", "眼", "耳",
    "呼吸", "咳嗽", "喷嚏", "发热", "发烧", "疼", "痛", "药", "疫苗", "驱虫",
)

# 进度回调函数类型别名
# 用于 SSE 流式推送时向客户端发送阶段完成事件
ProgressCallback: TypeAlias = Callable[[str, dict[str, Any]], Awaitable[None]]


class StreamSafetyAbort(Exception):
    """流式增量审核命中违规异常。

    【触发场景】
    在流式生成（_generate_streamed）过程中，每累积 60 个字符就运行一次
    诊断规则审核（DiagnosisRules.violations）。如果命中违规，立即抛出此异常。

    【处理流程】
    1. 流式生成捕获 StreamSafetyAbort
    2. 中断生成过程
    3. 调用方降级为固定安全模板（_fixed_safe_answer）
    4. 确保输出始终符合医疗安全规范

    【设计哲学】
    使用异常而非返回值，因为流式生成是异步迭代器，无法在中途返回错误状态。
    异常可以立即中断整个生成流程，避免继续输出违规内容。
    """


async def _emit_progress(
    progress: ProgressCallback | None,
    event: str,
    **data: Any,
) -> None:
    """安全发送进度事件（SSE 流式推送）。

    【职责】
    将处理阶段的完成事件推送到客户端（通过 SSE），让客户端可以实时显示进度。

    【容错设计】
    - progress 为 None 时直接返回（非流式模式）
    - 推送失败时记录警告，不阻断问诊主流程
    - 使用 try-except 确保进度推送异常不影响核心业务

    【使用场景】
    - 非流式模式：progress=None，不发送任何事件
    - 流式模式：progress=回调函数，通过 SSE 推送事件到客户端

    :param progress: 进度回调函数（SSE 推送用）
    :param event: 事件名称（如 "input_reviewed", "vision_completed"）
    :param data: 事件数据（字典，包含阶段详情）
    """
    if progress is None:
        return
    try:
        await progress(event, data)
    except Exception:  # noqa: BLE001 - progress delivery must not break consultation
        logger.warning("consult_progress_delivery_failed", exc_info=True)


class ConsultAgent:
    """问诊 Agent 主控制器（固定状态机编排）。

    【核心职责】
    1. 接收用户问诊请求（ConsultCommand）
    2. 按固定顺序执行 10+ 个处理阶段
    3. 协调外部服务（Vision、RAG、Guard、生成模型）
    4. 执行安全审核（输入审核、医疗审核、输出审核）
    5. 返回结构化问诊结果（ConsultResponse）

    【依赖注入】
    所有服务通过 __init__ 注入，由 Container 统一组装。
    必需依赖：settings、image_service、moderation_service 等
    可选依赖：rag_retriever、rag_emergency_matcher、dialogue_archive 等

    【状态管理】
    使用 ConsultState 作为状态总线，所有阶段的中间结果都写入 state。
    最终通过 _answer() 方法从 state 组装成 ConsultResponse。

    【超时管理】
    使用 DeadlineFactory 创建总预算，各阶段通过 child() 获取子预算。
    重试不得重新获得完整预算，防止某个阶段超时导致整个请求挂起。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        image_service: ImageService,
        moderation_service: ModerationService,
        input_moderator: InputModerator,
        output_moderator: OutputModerator,
        emergency_rules: EmergencyRuleEngine,
        conversation_service: ConversationService,
        completeness_checker: CompletenessChecker,
        risk_engine,
        consultation_service: ConsultationService,
        medical_safety_service: MedicalSafetyService,
        idempotency_repo: IdempotencyRepository,
        deadline_factory: DeadlineFactory,
        rag_retriever: ShadowRetriever | None = None,
        rag_emergency_matcher: V14EmergencyShadowMatcher | None = None,
        dialogue_archive: DialogueArchive | None = None,
        dialogue_repo: Any | None = None,
    ):
        """初始化 ConsultAgent（依赖注入）。

        【必需依赖】
        - settings: 应用配置
        - image_service: 图片分析服务（VisionGateway）
        - moderation_service: 审核服务（输入/输出审核）
        - input_moderator: 输入审核器
        - output_moderator: 输出审核器
        - emergency_rules: 急症规则引擎
        - conversation_service: 会话服务（历史加载/保存）
        - completeness_checker: 完整度检查器
        - risk_engine: 风险分级引擎
        - consultation_service: 问诊生成服务
        - medical_safety_service: 医疗安全服务
        - idempotency_repo: 幂等性存储
        - deadline_factory: 超时预算工厂

        【可选依赖】
        - rag_retriever: RAG 检索器（None 时跳过 RAG）
        - rag_emergency_matcher: RAG 急症匹配器（None 时跳过急症匹配）
        - dialogue_archive: 对话存档（JSONL 文件）
        - dialogue_repo: 对话存储（PostgreSQL）
        """
        self.s = settings
        self.image_service = image_service
        self.moderation = moderation_service
        self.input_moderator = input_moderator
        self.output_moderator = output_moderator
        self.emergency_rules = emergency_rules
        self.conversation_service = conversation_service
        self.completeness_checker = completeness_checker
        self.risk_engine = risk_engine
        self.consultation_service = consultation_service
        self.medical_safety_service = medical_safety_service
        self.idempotency_repo = idempotency_repo
        self.deadline_factory = deadline_factory
        self.rag_retriever = rag_retriever
        self.rag_emergency_matcher = rag_emergency_matcher
        self.dialogue_archive = dialogue_archive
        self.dialogue_repo = dialogue_repo

    # ============================================================
    # 入口方法（run / run_stream）
    # ============================================================

    async def run(
        self,
        command: ConsultCommand,
        *,
        progress: ProgressCallback | None = None,
    ) -> ConsultResponse:
        """非流式问诊入口。

        【职责】
        接收 ConsultCommand，执行完整问诊流程，返回 ConsultResponse。
        内部调用 _run_impl()，token_sink=None 表示非流式模式。

        :param command: 问诊命令（包含文字、图片、宠物信息等）
        :param progress: 进度回调函数（SSE 推送用）
        :return: ConsultResponse（问诊结果）
        """
        return await self._run_impl(command, progress=progress, token_sink=None)

    async def run_stream(
        self,
        command: ConsultCommand,
        *,
        progress: ProgressCallback | None = None,
        token_sink=None,
    ) -> ConsultResponse:
        """流式问诊入口。

        【职责】
        与 run() 相同，但生成阶段逐 token 经 token_sink 输出。
        用于 SSE 流式推送，客户端可以实时看到回答生成过程。

        :param command: 问诊命令
        :param progress: 进度回调函数
        :param token_sink: Token 接收器（SSE 推送用）
        :return: ConsultResponse（问诊结果）
        """
        return await self._run_impl(command, progress=progress, token_sink=token_sink)

    async def _run_impl(
        self,
        command: ConsultCommand,
        *,
        progress: ProgressCallback | None = None,
        token_sink=None,
    ) -> ConsultResponse:
        """问诊核心实现（非流式/流式共享）。

        【执行流程】
        1. 初始化状态总线（ConsultState.from_command）
        2. 推断物种（_apply_inferred_species）
        3. 创建 deadline 预算
        4. RAG 急症影子匹配（可选）
        5. 文字急症预判（纯规则，命中且无幂等键 → 直接返回固定急症模板）
        6. 幂等性检查（有幂等键 → 查询缓存/占位/轮询）
        7. 会话锁获取（防止并发请求）
        8. 执行主流程（_execute）
        9. 异常处理（超时/冲突/服务异常 → 急症优先返回固定模板）
        10. 缓存结果（幂等键 + 可缓存状态）
        11. 对话存档（JSONL + PG 双写）

        【容错设计】
        - 急症优先：任何异常下，如果是急症 → 返回固定急症模板
        - Redis 不可用 → 跳过幂等/锁，不阻断问诊
        - 超时 → 急症返回固定模板，否则返回错误

        :param command: 问诊命令
        :param progress: 进度回调
        :param token_sink: Token 接收器（None=非流式，有值=流式）
        :return: ConsultResponse
        """
        # 步骤 1：初始化状态总线（从命令反向隔离，不修改原始命令）
        state = ConsultState.from_command(command)
        # 步骤 2：推断物种（用户未明确物种时，从文字/历史推断）
        self._apply_inferred_species(state)
        # 步骤 3：创建 deadline 预算（总超时控制）
        # 根据命令中配置的总超时秒数，计算一个绝对超时时间点
        # 后续所有阶段都从这个总预算中分配子预算，防止某个阶段超时导致整个请求挂起
        deadline = self.deadline_factory.after_seconds(command.total_timeout_seconds)
        _run_t0 = __import__("time").monotonic()

        # 步骤 4：RAG 急症影子匹配（仅记录日志，不影响实际决策）
        # 用于验证新急症规则的准确率，结果写入日志供开发者分析
        if self.rag_emergency_matcher is not None:
            try:
                emergency_shadow = self.rag_emergency_matcher.search(
                    state.text,
                    species=state.pet_info.species if state.pet_info else None,
                )
                logger.info(
                    "rag_emergency_shadow_result",
                    extra={
                        "request_id": state.request_id,
                        "matched_rule_ids": list(emergency_shadow.matched_rule_ids),
                        "top_severity": emergency_shadow.top_severity,
                    },
                )
            except Exception:  # noqa: BLE001 - Shadow must not affect live triage
                # 影子匹配失败不影响主流程，仅记录警告
                logger.warning(
                    "rag_emergency_shadow_failed",
                    extra={"request_id": state.request_id},
                    exc_info=True,
                )

        # 步骤 5：文字急症预判（纯规则，不调外部服务）
        # 在一切模型依赖之前执行，确保急症场景下不依赖模型可用性
        # 无幂等键时直接固定返回；带幂等键时保留幂等冲突检查
        state.text_emergency_precheck = self.emergency_rules.precheck_text(
            text=state.text, pet_info=state.pet_info
        )
        precheck_emergency = state.text_emergency_precheck.level == RiskLevel.EMERGENCY
        # 
        if precheck_emergency and not command.idempotency_key:
            # 急症且无幂等键 → 直接返回固定急症模板，不走后续流程
            urgent = self._fixed_urgent_response(state)
            await _emit_progress(
                progress,
                "urgent_guidance",
                preliminary=False,
                response=urgent.model_dump(mode="json"),
            )
            return urgent

        # 步骤 6：幂等性检查（防止重复请求）
        # 基于请求内容计算唯一指纹，相同内容返回缓存结果
        # todo:防止用户不小心点了两次提交，或者网络超时不重复处理，直接返回第一次结果。
        owner = state.request_id
        request_hash = self._idempotency_fingerprint(command)
        # 哈希指纹
        idempotency_claimed = False
        if command.idempotency_key:
            try:
                cached = await self._idempotent_handoff(
                    command, owner, request_hash, deadline
                )
                if cached is not None:
                    # 缓存命中 → 直接返回缓存结果
                    return cached
                idempotency_claimed = True
            except RedisUnavailable as exc:
                # P0-2：Redis 不可用 → 跳过幂等优化，不阻断问诊
                state.degraded_services.append("redis")
                state.warnings.append(str(exc))

        # 步骤 7：会话锁获取（防止同一会话并发请求）
        # 构建 Redis 会话键，用于锁管理和历史存储
        key = self.conversation_service.key(
            state.tenant_id, state.user_id, state.conversation_id
        )
        lock_held = False
        try:
            # 急症请求跳过锁获取，直接返回固定模板
            if not precheck_emergency:
                try:
                    # 尝试获取会话锁（防止并发请求）
                    lock_held = await self.conversation_service.acquire_lock(
                        key, owner_token=owner
                    )
                    if not lock_held:
                        # 锁被占用 → 等待锁释放
                        lock_wait_started = __import__("time").monotonic()
                        logger.info(
                            "conversation_lock_wait_started",
                            extra={"request_id": state.request_id},
                        )
                        await self.conversation_service.wait_for_lock(
                            key, owner_token=owner
                        )
                        lock_held = True
                        logger.info(
                            "conversation_lock_acquired_after_wait",
                            extra={
                                "request_id": state.request_id,
                                "wait_ms": round(
                                    (__import__("time").monotonic() - lock_wait_started) * 1000
                                ),
                            },
                        )
                except ConversationConflictError:
                    # 等待锁超时 → 抛出冲突异常
                    logger.warning(
                        "conversation_lock_timeout",
                        extra={
                            "request_id": state.request_id,
                            "wait_seconds": self.s.conversation_lock_wait_seconds,
                        },
                    )
                    raise
                except RedisUnavailable as exc:
                    # Redis 不可用 → 无锁单轮继续（不阻断问诊）
                    state.degraded_services.append("redis")
                    state.warnings.append(str(exc))
                    lock_held = False

            # 步骤 8：执行主流程（_execute）
            try:
                if precheck_emergency:
                    # 急症 → 直接返回固定模板
                    response = self._fixed_urgent_response(state)
                    await _emit_progress(
                        progress,
                        "urgent_guidance",
                        preliminary=False,
                        response=response.model_dump(mode="json"),
                    )
                else:
                    # V1.1 P0-3：外层硬兜底，总请求不超绝对 deadline
                    # 使用 asyncio.wait_for 确保总超时控制
                    response = await asyncio.wait_for(
                        self._execute(
                            state, key, deadline,
                            progress=progress, token_sink=token_sink,
                        ),
                        timeout=deadline.require(),
                    )
            except asyncio.TimeoutError:
                raise RequestDeadlineExceeded("处理超时，请稍后重试") from None
            finally:
                # 释放会话锁（TTL 兜底自动过期）
                if lock_held:
                    try:
                        await self.conversation_service.release_lock(
                            key, owner_token=owner
                        )
                    except RedisUnavailable:
                        pass  # TTL 兜底自动过期
        # 步骤 9：异常处理（超时/冲突/服务异常 → 急症优先返回固定模板）
        except ConversationConflictError as exc:
            # 会话冲突（并发请求）→ 返回错误响应
            logger.warning(
                "conversation_conflict",
                extra={"request_id": state.request_id, "reason": str(exc)[:120]},
            )
            response = self._error(
                state, "CONVERSATION_CONFLICT", "会话正在处理中，请稍后重试", retryable=True
            )
        except RequestDeadlineExceeded:
            # 总超时 → 急症返回固定模板，否则返回错误
            if self._precheck_urgent(state):
                response = self._fixed_urgent_response(state)
            else:
                response = self._error(
                    state, "REQUEST_DEADLINE_EXCEEDED", "处理超时，请稍后重试",
                    retryable=True,
                )
        except ExternalServiceTimeout as exc:
            # 外部服务超时（Vision/RAG/生成模型等）
            logger.warning("外部服务超时: %s", exc)
            if self._precheck_urgent(state):
                response = self._fixed_urgent_response(state)
            else:
                response = self._error(
                    state, exc.code, str(exc), retryable=True
                )
        except Exception:  # noqa: BLE001 - 兜底：命中急症仍给固定模板
            # 未预期异常 → 急症优先返回固定模板
            logger.exception("consult_unhandled_error", extra={"request_id": state.request_id})
            if self._precheck_urgent(state):
                response = self._fixed_urgent_response(state)
            else:
                response = self._error(
                    state, "INTERNAL_ERROR", "服务内部错误，请稍后重试", retryable=True
                )

        # 步骤 10：缓存结果（幂等键 + 可缓存状态）
        if command.idempotency_key and idempotency_claimed:
            if self._cacheable_status(response):
                try:
                    await self.idempotency_repo.store_result(
                        state.tenant_id, state.user_id, command.idempotency_key,
                        request_hash, owner,
                        response.model_dump_json(),
                    )
                except RedisUnavailable:
                    # 缓存失败 → 释放占位，允许其他请求重新执行
                    await self.idempotency_repo.release_claim(
                        state.tenant_id, state.user_id, command.idempotency_key,
                        request_hash, owner,
                    )
            else:
                # P1-4：retryable 错误不缓存，释放占位让重试可重新执行
                await self.idempotency_repo.release_claim(
                    state.tenant_id, state.user_id, command.idempotency_key,
                    request_hash, owner,
                )
        # 步骤 11：对话存档（JSONL + PG 双写）
        total_ms = round((__import__("time").monotonic() - _run_t0) * 1000)
        await self._archive_dialogue(state, response, total_ms=total_ms)
        return response

    # ============================================================
    # 主流程执行（_execute）
    # ============================================================

    async def _execute(
        self,
        state: ConsultState,    # 状态总线（读写中间结果）
        key: str,        # 会话键（Redis 键名）
        deadline,
        *,
        progress: ProgressCallback | None = None,
        token_sink=None,
    ) -> ConsultResponse:
        """主流程执行（10+ 个阶段的状态机顺序执行）。

        【执行阶段】
        1. 文字急症预判（兜底，run() 已执行）
        2. 加载历史（Redis 不可用 → 无记忆单轮降级）
        3. 输入审核（Guard 审核用户输入）
        4. 非宠物问诊判断（高精度规则匹配）
        5. 图片解析（VisionGateway，失败不阻断）
        6. 图片异常检测（物种冲突/无宠物）
        7. RAG 检索（意图判断 + 追问查缺 + grounded 证据）
        8. 完整度判断（信息是否充足）
        9. 风险分级（合并文字预判 + 图片红旗 + 档案）
        10. 生成回答（三模式：normal/provisional/urgent）
        11. 医疗安全审核（失败 → 重写一次 → 仍失败 → 固定模板）
        12. 输出审核（Guard 审核生成内容）
        13. 组装响应 + 存历史

        【超时管理】
        - 每个阶段从 deadline 领取子预算
        - 重试不得重新获得完整预算
        - 总超时由 asyncio.wait_for 硬兜底

        :param state: 状态总线（读写中间结果）
        :param key: 会话键（Redis 键名）
        :param deadline: 超时预算对象
        :param progress: 进度回调
        :param token_sink: Token 接收器
        :return: ConsultResponse
        """
        import time as _time
        _t0 = _time.monotonic()     # 开始时间
        rid = state.request_id     # 请求 ID
        has_img = bool(state.image_inputs)     # 是否有图片
        
        # 全链路步骤耗时采集（2026-08-20）：挂到 state 引用，各 return 路径自动可见，
        # _archive_dialogue 写入 dialogue JSONL 供监控"中间经过哪些步骤"
        # 用于性能分析和问题排查
        steps: list[dict] = []
        state._steps = steps
        
        # 阶段 1：文字急症预判（最前置，纯规则；v6.3 §4 步骤 3）
        # run() 入口已执行（V1.1 P0-2）；此处仅兜底 _execute 被单独调用
        if state.text_emergency_precheck is None:
            state.text_emergency_precheck = self.emergency_rules.precheck_text(
                text=state.text, pet_info=state.pet_info
            )

        # 阶段 2：加载历史（Redis 不可用 → 无记忆单轮降级，v6.3 §28）
        try:
            snapshot = await self.conversation_service.load_context(key)
            state.history = snapshot.turns
            state.history_summary = snapshot.summary
        except RedisUnavailable as exc:
            # Redis 不可用 → 降级为无历史单轮对话
            state.degraded_services.append("redis")
            state.warnings.append(str(exc))

        # 本轮没有物种时，从同一会话最近一轮的宠物档案或用户文字继承。
        self._apply_inferred_species(state)
        logger.info(
            "pipeline_start",
            extra={
                "request_id": rid,
                "has_images": has_img,
                "text_len": len(state.text or ""),
                "text": (state.text or "")[:300],
                "pet_species": state.pet_info.species if state.pet_info else None,
                "species_source": getattr(state, "_species_source", None),
            },
        )

        # 3. 场景化输入审核（v6.3 §13.1.1：医疗求助不因 Violent 拒）
        guard_input_timeout = (
            deadline.require(cap=self.s.guard_input_timeout)
            if self.s.guard_enforced
            else None
        )
        _ti = _time.monotonic()
        state.input_moderation = await self.moderation.check_input(
            state.text,
            timeout_seconds=guard_input_timeout,
            request_id=state.request_id,
        )
        logger.info("stage_done", extra={"request_id": rid, "stage": "input_moderation", "ms": round((_time.monotonic() - _ti) * 1000)})
        steps.append({"stage": "input_moderation", "ms": round((_time.monotonic() - _ti) * 1000)})
        if state.input_moderation.parse_ok is False:
            # V1.1 P1-1：Guard 不可用/解析失败 → 保守 review（§28 不静默放行）；
            # 急症仍优先返回固定急症模板
            if self._precheck_urgent(state):
                return self._fixed_urgent_response(state)
            return await self._review(state, key)
        if state.input_moderation.should_refuse_medical_request:
            logger.info("request_refused", extra={"request_id": rid, "reason": "input_moderation"})
            return self._refuse(state)
        await _emit_progress(progress, "input_reviewed")

        # 明确的非宠物问诊请求直接分流，避免进入 RAG、医疗追问和生成链路。
        # 仅做高精度规则匹配；含宠物/症状上下文的问题（如“天气热狗狗喘”）仍正常问诊。
        if self._is_out_of_scope_query(state.text, has_images=has_img):
            response = self._fixed_out_of_scope_response(state)
            steps.append({"stage": "out_of_scope", "ms": 0})
            logger.info(
                "pipeline_end",
                extra={
                    "request_id": rid,
                    "status": "success",
                    "answer_mode": response.answer_mode.value,
                    "risk_level": response.risk_level.value,
                    "total_ms": round((_time.monotonic() - _t0) * 1000),
                    "answer_len": len(response.answer or ""),
                    "out_of_scope": True,
                },
            )
            await self._save_turn(state, key, response)
            return response

        # 阶段 5：图片解析（经 VisionGateway；V1.1 P0-3：阶段共享预算 15s）
        # 使用视觉模型分析图片，提取宠物症状信息
        # 失败不阻断问诊，只在回答中标注 degraded
        if state.image_inputs:
            await _emit_progress(
                progress,
                "vision_started",
                image_count=len(state.image_inputs),
            )
            vision_telemetry: list[dict] = []
            vision_degraded_reason: str | None = None
            _tv = _time.monotonic()
            try:
                state.vision_findings = await self.image_service.analyze(
                    state.image_inputs,
                    state.text or None,
                    deadline=deadline.child(cap=self.s.vision_timeout_seconds),
                    request_id=state.request_id,
                    telemetry=vision_telemetry,
                )
                logger.info("stage_done", extra={"request_id": rid, "stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "findings": len(state.vision_findings)})
                steps.append({"stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "findings": len(state.vision_findings)})
            except (VisionTimeout, VisionUnavailable, VisionOutputInvalid) as exc:
                vision_degraded_reason = f"{type(exc).__name__}: {str(exc)[:120]}"
                state.degraded_services.append("vision")
                state.warnings.append(str(exc))
                state.vision_findings = []
                logger.warning("stage_failed", extra={"request_id": rid, "stage": "vision", "error": str(exc)[:120]})
                steps.append({"stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "degraded": True, "error": str(exc)[:120]})
            except RequestDeadlineExceeded as exc:
                vision_degraded_reason = "RequestDeadlineExceeded"
                # Vision 使用独立的 15 秒 child deadline。子阶段耗尽时应放弃图片、
                # 继续使用总 deadline 的剩余预算；只有总预算也耗尽才终止请求。
                if not deadline.has_remaining(0.1):
                    raise
                state.degraded_services.append("vision")
                state.warnings.append(str(exc))
                state.vision_findings = []
                logger.warning("stage_failed", extra={"request_id": rid, "stage": "vision", "error": "deadline_exceeded"})
                steps.append({"stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "degraded": True, "error": "deadline_exceeded"})
            vision_ms = round((_time.monotonic() - _tv) * 1000)
            await _emit_progress(
                progress,
                "vision_completed",
                image_count=len(state.vision_findings),
                degraded="vision" in state.degraded_services,
                degraded_reason=vision_degraded_reason,
                ms=vision_ms,
                cache_hits=sum(1 for row in vision_telemetry if row.get("cache_hit")),
                queue_wait_ms=max(
                    (int(row.get("queue_wait_ms", 0)) for row in vision_telemetry),
                    default=0,
                ),
                inference_ms=max(
                    (int(row.get("inference_ms", 0)) for row in vision_telemetry),
                    default=0,
                ),
                gateway_total_ms=max(
                    (int(row.get("gateway_total_ms", 0)) for row in vision_telemetry),
                    default=0,
                ),
                format_retries=sum(
                    int(row.get("format_retries", 0)) for row in vision_telemetry
                ),
            )

        vision_failed = bool(
            state.image_inputs
            and not state.vision_findings
            and "vision" in state.degraded_services
        )
        has_text_context = bool(
            state.text.strip() or any(t.user_text.strip() for t in state.history)
        )
        if vision_failed and not has_text_context:
            # 只有图片且图片服务不可用时没有可供生成的事实，禁止调用模型猜测。
            return await self._review(state, key, reason="image_unavailable")

        # 阶段 5.5：图片异常检测（v1.2 §4.6：图文物种冲突 / 无宠物）
        # 检查图片中是否有宠物，以及图片物种是否与文字描述一致
        species_conflict = self._detect_species_conflict(state)
        no_pet = self._detect_no_pet(state)

        # 阶段 6：RAG 检索（意图判断 + 追问查缺 + grounded 证据）
        # 一次检索同时完成多个任务：判断意图、生成追问、提取证据
        rag_questions: list[str] = []
        _trg = _time.monotonic()
        if self.rag_retriever is not None:
            try:
                state.rag_result = self.rag_retriever.search(
                    state.text,
                    species=state.pet_info.species if state.pet_info else None,
                )
                if self.s.rag_grounded:
                    state.rag_evidence = self.rag_retriever.build_grounded_evidence(
                        state.rag_result
                    )
                if (
                    self.s.rag_followup_check
                    and state.rag_result.decision is RagDecisionStatus.SUFFICIENT
                    and state.rag_result.hits
                ):
                    top_card = self._top_card(state.rag_result)
                    rag_questions = [
                        str(q) for q in top_card.get("questions_to_ask", []) if q
                    ][:3]
                logger.info(
                    "rag_result",
                    extra={
                        "request_id": rid,
                        "decision": state.rag_result.decision.value,
                        "reason_codes": state.rag_result.reason_codes,
                        "hit_ids": [hit.card_id for hit in state.rag_result.hits],
                        "top_score": state.rag_result.top_score,
                        "retrieval_ms": round(state.rag_result.retrieval_ms, 2),
                        "grounded_cards": [item["card_id"] for item in state.rag_evidence],
                        "rag_questions": rag_questions,
                        "pet_species": state.pet_info.species if state.pet_info else None,
                    },
                )
            except Exception:  # noqa: BLE001 - 检索失败不得影响问诊主链路
                logger.warning("rag_failed", extra={"request_id": rid}, exc_info=True)
        steps.append({"stage": "rag", "ms": round((_time.monotonic() - _trg) * 1000), "hits": len(state.rag_result.hits) if state.rag_result else 0})

        # 阶段 7：完整度判断（带卡片追问查缺；不阻断回答，只决定 provisional）
        # 评估用户提供的信息是否充足，生成追问问题
        state.completeness = self.completeness_checker.evaluate(
            state, rag_questions=rag_questions
        )
        if vision_failed:
            # 用户明确上传了图片但本轮未能观察，回答必须标注为 provisional。
            state.completeness.need_more_info = True
            state.completeness.questions = list(
                dict.fromkeys(
                    state.completeness.questions
                    + ["本次未能解析图片，请重新上传清晰图片或补充文字描述。"]
                )
            )
        if species_conflict:
            # 图文物种冲突：以文字为准，追问确认前不采信图片观察
            state.completeness.need_more_info = True
            conflict_msg = (
                "您上传的照片中看到的宠物与您描述的不一致，请确认照片是否为同一只宠物，"
                "或重新上传对应的照片。"
            )
            state.completeness.questions = list(
                dict.fromkeys([conflict_msg] + state.completeness.questions)
            )
            state.warnings.append("species_conflict")
            # 冲突未确认前以文字为准：图片观察不参与风险与生成
            state.vision_findings = []
        elif no_pet and not has_text_context:
            state.completeness.need_more_info = True
            state.completeness.questions = [
                "这张照片里没有识别到宠物，请重新拍摄您的宠物照片，或用文字描述情况。"
            ]
        elif no_pet:
            # 有文字：忽略图片，继续文字问诊
            state.degraded_services.append("vision_no_pet")
            state.warnings.append("图片中未识别到宠物，已按文字描述回答")
            state.vision_findings = []

        # 阶段 8：最终风险分级（合并文字预判 + 图片红旗 + 档案）
        # 综合所有信息评估风险等级：EMERGENCY/HIGH/MEDIUM/LOW
        state.emergency_result = self.emergency_rules.evaluate(
            text=self._combined_user_text(state),
            red_flags=[f for f in state.vision_findings for f in f.red_flags],
            pet_info=state.pet_info,
            precheck=state.text_emergency_precheck,
        )
        _tr = _time.monotonic()
        state.risk_result = self.risk_engine.evaluate(state)
        logger.info("stage_done", extra={"request_id": rid, "stage": "risk_assess", "ms": round((_time.monotonic() - _tr) * 1000), "risk_level": state.risk_result.level.value, "urgent": state.emergency_result.force_urgent_guidance})
        steps.append({"stage": "risk_assess", "ms": round((_time.monotonic() - _tr) * 1000), "risk_level": state.risk_result.level.value})
        await _emit_progress(progress, "risk_assessed")
        if state.risk_result.level == RiskLevel.EMERGENCY:
            urgent = self._fixed_urgent_response(state)
            await _emit_progress(
                progress,
                "urgent_guidance",
                preliminary=False,
                response=urgent.model_dump(mode="json"),
            )
            await self._save_turn(state, key, urgent)
            return urgent

        # 6.5 直答通道（v1.2 §4.3：简单问答卡片直答，不调生成模型）
        # 2026-08-19：信息不足（need_more_info）时不直答，走追问（§4.7 查缺闭环）
        if (
            self.s.rag_fast_answer
            and self.rag_retriever is not None
            and state.rag_result is not None
            and state.risk_result.level in (RiskLevel.LOW, RiskLevel.MEDIUM)
            # 信息不足时不进入卡片直答，先给简短初步建议并自然追问。
            and not (
                state.completeness.need_more_info
                and state.completeness.reason
                in ("hard_need", "pet_ambiguous", "keyword_thin")
            )
            and self.rag_retriever.is_fast_answerable(state.rag_result)
        ):
            fast = await self._fast_answer(state, key, progress=progress)
            logger.info("pipeline_end", extra={"request_id": rid, "status": "fast_answer", "answer_mode": "normal", "risk_level": state.risk_result.level.value, "total_ms": round((_time.monotonic() - _t0) * 1000), "answer_len": len(fast.answer or "")})
            return fast

        # 6.6 多宠歧义固定追问（2026-08-19）：不确定问哪只时直接返回友好追问模板，
        # 不调生成模型（避免模型生成"对象混淆风险"类生硬措辞），下一轮用户回答后正常问诊
        if (
            state.completeness is not None
            and state.completeness.need_more_info
            and state.completeness.reason == "pet_ambiguous"
        ):
            resp = self._fixed_pet_ambiguous_response(state)
            logger.info("pipeline_end", extra={"request_id": rid, "status": "success", "answer_mode": resp.answer_mode.value, "risk_level": state.risk_result.level.value, "total_ms": round((_time.monotonic() - _t0) * 1000), "answer_len": len(resp.answer or ""), "pet_ambiguous": True})
            return resp

        # 阶段 9：生成回答（三模式；EMERGENCY 已在上方固定短路）
        # 根据风险等级和信息完整度选择不同的生成模式：
        # - normal：正常问诊（信息充足，低风险）
        # - provisional：初步建议（信息不足，需要追问）
        # - urgent_guidance：紧急指导（高风险）
        _tg = _time.monotonic()
        try:
            if token_sink is not None:
                # 流式模式：逐 token 生成，实时推送
                mode = await self._generate_streamed(
                    state, deadline, token_sink,
                    request_id=rid,
                )
            elif state.risk_result.level == RiskLevel.HIGH:
                # 高风险模式：生成紧急指导（不短路，仍走生成流程）
                state.generated = await self.consultation_service.generate_urgent_guidance(
                    state, deadline=deadline.child(cap=20.0)
                )
                mode = "urgent_guidance"
            elif (
                state.completeness.need_more_info
                and state.completeness.reason in ("hard_need", "keyword_thin")
            ):
                # 硬性缺失或短症状信息不足：先给简短初步建议，再提出关键追问
                state.generated = await self.consultation_service.generate_provisional(
                    state, deadline=deadline.child(cap=20.0)
                )
                mode = "provisional"
            else:
                # 正常模式：信息充足，生成完整回答
                state.generated = await self.consultation_service.generate(
                    state, deadline=deadline.child(cap=20.0)
                )
                mode = "normal"
            logger.info("stage_done", extra={"request_id": rid, "stage": "generate", "mode": mode, "ms": round((_time.monotonic() - _tg) * 1000)})
            steps.append({"stage": "generate", "mode": mode, "ms": round((_time.monotonic() - _tg) * 1000)})
        except StreamSafetyAbort as exc:
            # 流式增量审核命中违规：立即切换固定安全模板
            # 防止模型输出不安全的医疗建议
            state.degraded_services.append("safety_stream_abort")
            state.warnings.append(str(exc))
            state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
            logger.warning(
                "stage_failed",
                extra={"request_id": rid, "stage": "generate", "error": "stream_safety_abort"},
            )
        except KnowledgeConsultUnavailable as exc:
            # 本地 9B 偶发输出解析失败: deadline 内重试一次(2026-08-18)
            # 但模型超时通常表示容量已经饱和，此时立即重试会形成重试风暴
            if (
                _should_retry_knowledge_failure(exc)
                and not getattr(state, "_generate_retried", False)
                and deadline.has_remaining(12.0)
            ):
                # 满足重试条件：非超时类故障 + 未重试过 + 剩余预算充足
                state._generate_retried = True
                logger.warning(
                    "stage_generate_retry",
                    extra={"request_id": rid, "error": str(exc)[:120]},
                )
                try:
                    if state.risk_result.level == RiskLevel.HIGH:
                        state.generated = await self.consultation_service.generate_urgent_guidance(
                            state, deadline=deadline.child(cap=15.0)
                        )
                        mode = "urgent_guidance"
                    elif (
                        state.completeness.need_more_info
                        and state.completeness.reason in ("hard_need", "keyword_thin")
                    ):
                        # 与主分支一致：信息不足时重试仍走 provisional
                        state.generated = await self.consultation_service.generate_provisional(
                            state, deadline=deadline.child(cap=15.0)
                        )
                        mode = "provisional"
                    else:
                        state.generated = await self.consultation_service.generate(
                            state, deadline=deadline.child(cap=15.0)
                        )
                        mode = "normal"
                    logger.info(
                        "stage_done",
                        extra={"request_id": rid, "stage": "generate", "mode": mode, "retry": True,
                               "ms": round((_time.monotonic() - _tg) * 1000)},
                    )
                    steps.append({"stage": "generate_retry", "mode": mode, "ms": round((_time.monotonic() - _tg) * 1000)})
                except KnowledgeConsultUnavailable as exc2:
                    # 重试仍然失败 → 返回服务不可用
                    state.degraded_services.append("knowledge_consult")
                    state.warnings.append(str(exc2))
                    logger.warning(
                        "stage_failed",
                        extra={"request_id": rid, "stage": "generate", "error": str(exc2)[:120]},
                    )
                    return self._service_unavailable(state, str(exc2))
            else:
                # 不满足重试条件：超时类故障或预算不足 → 直接返回错误
                state.degraded_services.append("knowledge_consult")
                state.warnings.append(str(exc))
                logger.warning(
                    "stage_failed",
                    extra={
                        "request_id": rid,
                        "stage": "generate",
                        "error": str(exc)[:120],
                        "retry_skipped": not _should_retry_knowledge_failure(exc),
                    },
                )
                return self._service_unavailable(state, str(exc))
        generate_ms = round((_time.monotonic() - _tg) * 1000)
        await _emit_progress(progress, "answer_generated", ms=generate_ms)

        # 8. 医疗安全后置检查 + 受 deadline 限制的重写一次（v6.3 §16.2）
        _tm = _time.monotonic()
        state.medical_review = await self.medical_safety_service.review(
            state.generated,
            red_flags=[f for f in state.vision_findings for f in f.red_flags],
            expected_risk=state.risk_result.level,
            expected_urgency=state.risk_result.vet_urgency,
        )
        # 可确定性修复的规则问题只修改违规字段，保留症状、图片观察和护理建议。
        # 这一步避免“重写超时 → 整段固定模板”吞掉原本正确的针对性回答。
        if not state.medical_review.passed:
            repaired = self.medical_safety_service.repair_locally(
                state.generated,
                violations=state.medical_review.violations,
                expected_risk=state.risk_result.level,
                expected_urgency=state.risk_result.vet_urgency,
            )
            if repaired is not None:
                state.generated = repaired
                state.medical_review = await self.medical_safety_service.review(
                    state.generated,
                    red_flags=[
                        flag
                        for finding in state.vision_findings
                        for flag in finding.red_flags
                    ],
                    expected_risk=state.risk_result.level,
                    expected_urgency=state.risk_result.vet_urgency,
                )
                logger.info(
                    "stage_done",
                    extra={
                        "request_id": rid,
                        "stage": "safety_local_repair",
                        "passed": state.medical_review.passed,
                        "violations": state.medical_review.violations,
                    },
                )
        _rewrite_cap = self.s.safety_rewrite_timeout_seconds
        if not state.medical_review.passed and deadline.has_remaining(_rewrite_cap):
            try:
                state.generated = await self.consultation_service.rewrite_once(
                    state, state.generated, state.medical_review.violations,
                    deadline=deadline.child(cap=_rewrite_cap),
                )
                state.medical_review = await self.medical_safety_service.review(
                    state.generated,
                    red_flags=[f for f in state.vision_findings for f in f.red_flags],
                    expected_risk=state.risk_result.level,
                    expected_urgency=state.risk_result.vet_urgency,
                )
                if not state.medical_review.passed:
                    repaired = self.medical_safety_service.repair_locally(
                        state.generated,
                        violations=state.medical_review.violations,
                        expected_risk=state.risk_result.level,
                        expected_urgency=state.risk_result.vet_urgency,
                    )
                    if repaired is not None:
                        state.generated = repaired
                        state.medical_review = await self.medical_safety_service.review(
                            state.generated,
                            red_flags=[
                                flag
                                for finding in state.vision_findings
                                for flag in finding.red_flags
                            ],
                            expected_risk=state.risk_result.level,
                            expected_urgency=state.risk_result.vet_urgency,
                        )
            except Exception as exc:  # noqa: BLE001 - 安全重写失败必须固定兜底
                state.degraded_services.append("safety_rewrite")
                state.warnings.append(str(exc))
                logger.warning(
                    "stage_failed",
                    extra={
                        "request_id": rid,
                        "stage": "safety_rewrite",
                        "error": str(exc)[:120],
                    },
                )
        if not state.medical_review.passed:
            state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
        logger.info(
            "stage_done",
            extra={
                "request_id": rid,
                "stage": "medical_review",
                "ms": round((_time.monotonic() - _tm) * 1000),
                "passed": state.medical_review.passed,
                "violations": state.medical_review.violations,
            },
        )
        steps.append({"stage": "medical_review", "ms": round((_time.monotonic() - _tm) * 1000), "passed": state.medical_review.passed})
        medical_review_ms = round((_time.monotonic() - _tm) * 1000)
        await _emit_progress(
            progress,
            "medical_review_completed",
            ms=medical_review_ms,
        )

        # RAG 无可用证据且信息不足时，模型偶尔仍会在正文或就医理由中自行枚举
        # 具体疾病/专科方向。此处做确定性收口，并清空 answer_text 让响应基于
        # 收口后的结构化字段重新渲染，避免模型正文残留未经证据支持的病因。
        if self._apply_provisional_no_evidence_guard(state):
            logger.info(
                "provisional_no_evidence_guard",
                extra={
                    "request_id": rid,
                    "rag_decision": state.rag_result.decision.value,
                    "reason_codes": state.rag_result.reason_codes,
                },
            )

        # 提取 RAG 检索结果的分类信息（用于后续输出过滤）
        rag_categories: tuple[str, ...] = ()
        if self.rag_retriever is not None and state.rag_result is not None:
            hit_ids = {hit.card_id for hit in state.rag_result.hits}
            rag_categories = tuple(
                dict.fromkeys(
                    str(card.get("category", ""))
                    for card in self.rag_retriever.report.cards
                    if card.get("id") in hit_ids and card.get("category")
                )
            )
        
        # 清理面向用户的语言（去除兽医专业术语）
        # 例如：去掉"建议转诊眼科"等不适合宠物主人的表述
        state.generated, language_cleaned = self.medical_safety_service.clean_owner_facing_language(
            state.generated,
            is_eye_case=state.case_facts.domain == "eye",
            user_text=state.text,
            rag_categories=rag_categories,
        )
        if language_cleaned:
            logger.info(
                "owner_facing_language_cleaned",
                extra={"request_id": rid},
            )

        # 阶段 11：输出通用审核（场景化；受同一 deadline 约束）
        # 使用 Guard 审核生成的回答内容，检查是否包含违规或不安全内容
        _to = _time.monotonic()
        guard_output_timeout = (
            deadline.require(cap=self.s.guard_output_timeout)
            if self.s.guard_enforced
            else None
        )
        state.output_moderation = await self.moderation.check_output(
            state.generated,
            timeout_seconds=guard_output_timeout,
            request_id=state.request_id,
        )
        logger.info("stage_done", extra={"request_id": rid, "stage": "output_moderation", "ms": round((_time.monotonic() - _to) * 1000), "blocked": state.output_moderation.blocked})
        steps.append({"stage": "output_moderation", "ms": round((_time.monotonic() - _to) * 1000), "blocked": state.output_moderation.blocked})
        if state.output_moderation.blocked:
            # 输出被审核拦截 → 进入人工审核流程
            return await self._review(state, key)
        output_moderation_ms = round((_time.monotonic() - _to) * 1000)
        await _emit_progress(
            progress,
            "output_review_completed",
            ms=output_moderation_ms,
        )

        # 阶段 12：组装响应 + 存历史
        # 将所有结构化数据渲染为最终回答文本，保存到对话历史
        response = await self._answer(state, key)
        logger.info("pipeline_end", extra={"request_id": rid, "status": response.status.value, "answer_mode": response.answer_mode, "risk_level": response.risk_level.value if response.risk_level else "N/A", "total_ms": round((_time.monotonic() - _t0) * 1000), "answer_len": len(response.answer or ""), "answer_preview": (response.answer or "")[:300]})
        return response

    # ============================================================
    # 流式生成（_generate_streamed）
    # ============================================================

    async def _generate_streamed(
        self,
        state: ConsultState,
        deadline,
        token_sink,
        *,
        request_id: str,
    ) -> str:
        """流式生成（v1.4 两段式）：token 逐块转发 + 增量安全审核。

        【核心职责】
        1. 根据风险等级选择生成模式（urgent_guidance/provisional/normal）
        2. 逐 token 转发到 token_sink（SSE 推送）
        3. 增量安全审核（每 60 字符检查一次）
        4. 命中违规立即中断（抛 StreamSafetyAbort）

        【增量审核机制】
        - 每累积 60 字符运行一次 DiagnosisRules.violations()
        - 追加"。本回答仅供参考。"后检查（模拟完整回答）
        - 命中违规 → 抛 StreamSafetyAbort → 调用方降级固定模板
        - 确保流式输出也符合医疗安全规范

        【容错设计】
        - 流式失败 → 回退非流式生成（generate 方法）
        - StreamSafetyAbort → 立即中断，由调用方处理
        - 生成完成后更新 state.generated.answer_text（完整回答）

        :param state: 状态总线
        :param deadline: 超时预算
        :param token_sink: Token 接收器（SSE 推送用）
        :param request_id: 请求 ID
        :return: 生成模式（urgent_guidance/provisional/normal）
        """
        from app.safety.diagnosis_rules import DiagnosisRules

        if state.risk_result.level == RiskLevel.HIGH:
            mode = "urgent_guidance"
            generate = self.consultation_service.generate_urgent_guidance
        elif (
            state.completeness.need_more_info
            and state.completeness.reason
            in ("hard_need", "pet_ambiguous", "keyword_thin")
        ):
            mode = "provisional"
            generate = self.consultation_service.generate_provisional
        else:
            mode = "normal"
            generate = self.consultation_service.generate
        # 构建咨询请求
        request = self.consultation_service.knowledge_consult.build_request(state, mode)
        # 获取咨询适配器
        adapter = self.consultation_service.knowledge_consult.adapter
        answer_parts: list[str] = []
        checked_until = 0
        try:
            # 流式生成咨询回答
            async for kind, value in adapter.generate_consultation_stream(
                request=request,
                deadline=deadline.child(cap=20.0),
                request_id=request_id,
            ):
                if kind == "token":
                    text = str(value)
                    answer_parts.append(text)
                    await token_sink(text)
                    joined = "".join(answer_parts)
                    if len(joined) - checked_until >= 60:
                        checked_until = len(joined)
                        if DiagnosisRules.violations(joined + "。本回答仅供参考。"):
                            raise StreamSafetyAbort("流式增量审核命中违规") from None
                else:
                    state.generated = value
        except StreamSafetyAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - 流式失败回退非流式
            logger.warning(
                "stream_failed_fallback_non_stream",
                extra={"request_id": request_id, "error": str(exc)[:160]},
            )
            state.generated = await generate(state, deadline=deadline.child(cap=20.0))
            return mode
        full_answer = "".join(answer_parts).strip()
        if full_answer and state.generated is not None:
            state.generated.answer_text = full_answer
        return mode


    # ============================================================
    # 直答/图片异常/存档
    # ============================================================

    async def _fast_answer(
        self, state: ConsultState, key, *, progress: ProgressCallback | None = None
    ) -> ConsultResponse:
        """简单问答直答通道：卡片内容渲染，不调生成模型（v1.2 §4.3）。

        【核心职责】
        1. 从 RAG 检索结果中提取 simple_owner_question 卡片
        2. 渲染卡片内容为 GeneratedConsultation
        3. 走医疗检查 + 输出审核（保证安全兜底一致）
        4. 返回 ConsultResponse

        【触发条件】
        - rag_fast_answer 配置启用
        - RAG 检索结果命中 simple_owner_question 卡片
        - 置信度超阈值
        - 风险等级为 LOW/MEDIUM
        - 信息充足（need_more_info=False）

        【安全保证】
        - 仍需走规则级医疗检查（ms 级）
        - 仍需走输出审核
        - 医疗检查失败 → 固定安全模板

        :param state: 状态总线
        :param key: 会话键
        :param progress: 进度回调
        :return: ConsultResponse（直答结果）
        """
        from app.core.constants import AnswerMode
        from app.schemas.consult import GeneratedConsultation, VetRecommendation

        assert self.rag_retriever is not None and state.rag_result is not None
        payload = self.rag_retriever.build_fast_answer(
            state.rag_result,
            query_species=(
                normalize_species(state.pet_info.species) if state.pet_info else None
            ),
        )
        # 构建直答结果
        state.generated = GeneratedConsultation(
            summary=payload["summary"],
            what_to_do_now=payload["what_to_do_now"],
            follow_up_questions=payload["follow_up_questions"],
            possible_explanations=payload["possible_explanations"],
            avoid_actions=payload["avoid_actions"],
            what_to_monitor=payload["what_to_monitor"],
            risk_level=state.risk_result.level,
            answer_mode=AnswerMode.NORMAL,
            self_reported_confidence=None,
            vet_recommendation=VetRecommendation(
                recommended=False, urgency=VetUrgency.NONE
            ),
            disclaimer=DEFAULT_DISCLAIMER,
        )
        # 走医疗检查
        state.medical_review = await self.medical_safety_service.review(
            state.generated,
            red_flags=[],
            expected_risk=state.risk_result.level,
            expected_urgency=state.risk_result.vet_urgency,
        )
        if not state.medical_review.passed:
            state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
        await _emit_progress(progress, "answer_generated")
        await _emit_progress(progress, "medical_review_completed")
        state.output_moderation = await self.moderation.check_output(
            state.generated,
            timeout_seconds=None,
            request_id=state.request_id,
        )
        if state.output_moderation.blocked:
            return await self._review(state, key)
        await _emit_progress(progress, "output_review_completed")
        return await self._answer(state, key)

    def _top_card(self, result) -> dict:
        if not result or not result.hits or self.rag_retriever is None:
            return {}
        cards = {c["id"]: c for c in self.rag_retriever.report.cards}
        return cards.get(result.hits[0].card_id, {})

    def _detect_species_conflict(self, state: ConsultState) -> bool:
        """图文物种冲突：图片观察到的物种与文字/档案物种不一致（v1.2 §4.6）。

        仅对真实 Vision 输出生效（mock 观察不参与冲突判定）。
        """
        if self.s.mock_vision or not state.vision_findings or not state.pet_info:
            return False
        text_species = (state.pet_info.species or "").strip()
        if "狗" in text_species or "犬" in text_species:
            text_norm = "dog"
        elif "猫" in text_species:
            text_norm = "cat"
        else:
            return False
        vision_species = {
            f.species_guess for f in state.vision_findings
            if f.species_guess in ("cat", "dog")
        }
        return bool(vision_species and text_norm not in vision_species)

    @staticmethod
    def _detect_no_pet(state: ConsultState) -> bool:
        """图片中没有宠物：物种 unknown 且无任何观察（v1.2 §4.6）。"""
        return any(
            f.species_guess == "unknown" and not f.observations
            for f in state.vision_findings
        )

    @staticmethod
    def _apply_provisional_no_evidence_guard(state: ConsultState) -> bool:
        """真正的模糊状态描述且无证据时，禁止输出未经支持的具体病因。

        【触发条件】
        - generated.answer_mode == PROVISIONAL（初步建议模式）
        - rag_result.decision != SUFFICIENT（信息不足）
        - rag_result.reason_codes 包含 "vague_general_query"（模糊查询）
        - 没有 RAG 证据（state.rag_evidence 为空）
        - 没有图片观察结果（state.vision_findings 为空）

        【保护逻辑】
        - 清空 possible_explanations（避免无根据的病因猜测）
        - 设置通用 summary（"状态不佳，信息有限"）
        - 清空 answer_text（强制使用 _render 渲染）

        【设计意图】
        - 防止模型在信息不足时"编造"具体病因
        - 确保回答保守、安全、不误导用户
        - 图片有可用观察时，不得用通用"状态不佳"覆盖视觉结论

        :param state: 状态总线
        :return: True 如果应用了保护逻辑，False 否则
        """
        generated = state.generated
        rag_result = state.rag_result
        if generated is None or rag_result is None:
            return False
        if generated.answer_mode is not AnswerMode.PROVISIONAL:
            return False
        if rag_result.decision is RagDecisionStatus.SUFFICIENT:
            return False
        if "vague_general_query" not in rag_result.reason_codes:
            return False
        if state.rag_evidence:
            return False
        # 图片已经给出可用观察时，不得用通用“状态不佳”覆盖视觉结论。
        if state.vision_findings:
            return False

        species = normalize_species(state.pet_info.species if state.pet_info else None)
        subject = {"dog": "狗狗", "cat": "猫咪"}.get(species, "宠物")
        generated.summary = (
            f"{subject}目前状态不佳，但现有信息有限，暂时无法判断具体原因。"
            "请继续观察并补充症状持续时间、精神食欲和活动情况。"
        )
        generated.possible_explanations = []
        generated.vet_recommendation.reason = (
            "由于目前信息有限，暂时无法判断具体原因。若状态持续、加重或出现其他异常，"
            "建议及时就医检查。"
        )
        generated.answer_text = ""
        return True

    @staticmethod
    def _is_out_of_scope_query(text: str, *, has_images: bool = False) -> bool:
        """识别高置信的非宠物问诊问题；宁可少拦，不误拦宠物健康语境。

        【判断逻辑】
        1. 有图片 → 永远不算 out of scope（图片可能包含宠物）
        2. 文字包含宠物语境词（_PET_CONTEXT_TERMS） → 不算 out of scope
        3. 文字匹配非宠物模式（_OUT_OF_SCOPE_PATTERNS） → 算 out of scope

        【典型 out of scope 问题】
        - 天气查询、股票查询
        - 人类医疗问题
        - 与宠物无关的闲聊

        【设计原则】
        - 宁可少拦（不拦截边界情况），不误拦（不拦截真正的宠物问题）
        - 有宠物语境词时，即使问题奇怪也不算 out of scope

        :param text: 用户输入文字
        :param has_images: 是否有图片
        :return: True 如果是非宠物问诊，False 否则
        """
        if has_images:
            return False
        normalized = re.sub(r"[\s，。！？、,.!?]+", "", (text or "").lower())
        if not normalized or any(term in normalized for term in _PET_CONTEXT_TERMS):
            return False
        return any(pattern.fullmatch(normalized) for pattern in _OUT_OF_SCOPE_PATTERNS)

    async def _archive_dialogue(
        self, state: ConsultState, response: ConsultResponse, *, total_ms: float | None = None
    ) -> None:
        """效果观测存档（v1.2 §7）：JSONL + PG 双写，失败不影响主链路。

        【核心职责】
        1. 构建对话记录（包含请求信息、响应结果、中间状态）
        2. 写入 JSONL 文件（用于监控和分析）
        3. 写入 PostgreSQL（用于查询和统计）
        4. 失败不阻断主流程（仅记录警告）

        【存档内容】
        - 请求信息：request_id, tenant_id, user_id, conversation_id
        - 用户输入：user_text, pet_info, image_findings
        - 响应结果：status, answer_mode, risk_level, answer
        - 中间状态：rag_decision, degraded_services, steps
        - 性能数据：total_ms, steps（各阶段耗时）

        【使用场景】
        - 效果观测：分析回答质量、风险分布、降级率
        - 问题排查：定位失败原因、分析超时瓶颈
        - 模型优化：收集 bad case，优化 prompt 和检索策略

        :param state: 状态总线
        :param response: 问诊响应
        :param total_ms: 总耗时（毫秒）
        """
        if (self.dialogue_archive is None or not self.dialogue_archive.enabled) and self.dialogue_repo is None:
            return
        
        # 构建完整的对话记录（包含所有中间状态）
        record = {

                "ts": utc_now_iso(),
                "request_id": state.request_id,
                "tenant_id": state.tenant_id,
                "user_id": state.user_id,
                "conversation_id": state.conversation_id,
                "user_text": state.text,
                "pet_info": state.pet_info.model_dump() if state.pet_info else None,
                "image_findings": [f.model_dump(mode="json") for f in state.vision_findings],
                "status": response.status.value,
                "answer_mode": response.answer_mode.value if response.answer_mode else None,
                "risk_level": response.risk_level.value if response.risk_level else None,
                "risk_flags": response.risk_flags,
                "hit_card_ids": (
                    [h.card_id for h in state.rag_result.hits] if state.rag_result else []
                ),
                "rag_decision": state.rag_result.decision.value if state.rag_result else "",
                "rag_top_score": state.rag_result.top_score if state.rag_result else None,
                "degraded_services": state.degraded_services,
                "follow_up_questions": response.follow_up_questions,
                "answer": response.answer,
                "total_ms": total_ms,
                # 全链路步骤耗时（2026-08-20）：input_moderation/vision/rag/risk_assess/
                # generate/generate_retry/medical_review/output_moderation
                "steps": [dict(s) for s in getattr(state, "_steps", [])],
            }
        if self.dialogue_archive is not None and self.dialogue_archive.enabled:
            await self.dialogue_archive.write(record)
        if self.dialogue_repo is not None:
            await self.dialogue_repo.write(record)

    # ============================================================
    # 响应分支（_answer / _refuse / _review / _error / _service_unavailable）
    # ============================================================

    async def _answer(self, state: ConsultState, key) -> ConsultResponse:
        """组装最终响应（成功路径）。

        【核心职责】
        1. 从 state.generated 提取结构化字段
        2. 计算最终风险等级（取 state.risk_result 和 g.risk_level 的最大值）
        3. 渲染回答文本（g.answer_text 或 self._render(g)）
        4. 合并追问（completeness.questions + generated.follow_up_questions）
        5. 保存对话历史（_save_turn）
        6. 返回 ConsultResponse（status=SUCCESS）

        【回答渲染优先级】
        - 优先使用 g.answer_text（模型生成的完整回答）
        - 如果 g.answer_text 为空，使用 self._render(g)（结构化字段拼装）

        【风险等级计算】
        - 使用 risk_engine.max_level() 取最大值
        - 确保最终风险不低于任何阶段的风险评估

        :param state: 状态总线
        :param key: 会话键
        :return: ConsultResponse（成功响应）
        """
        g = state.generated
        assert g is not None
        level = self.risk_engine.max_level(state.risk_result.level, g.risk_level)
        response = ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.SUCCESS,
            answer_mode=g.answer_mode,
            answer=(g.answer_text or self._render(g)),
            summary=g.summary,
            possible_explanations=g.possible_explanations,
            what_to_do_now=g.what_to_do_now,
            avoid_actions=g.avoid_actions,
            what_to_monitor=g.what_to_monitor,
            risk_level=level,
            risk_flags=self._risk_flags(state),
            vet_recommendation=g.vet_recommendation,
            image_findings=state.vision_findings,
            follow_up_questions=self._merge_questions(state, g.follow_up_questions),
            self_reported_confidence=g.self_reported_confidence,
            disclaimer=g.disclaimer or DEFAULT_DISCLAIMER,
            knowledge_degraded="knowledge_consult" in state.degraded_services,
        )
        await self._save_turn(state, key, response)
        return response

    def _refuse(self, state: ConsultState) -> ConsultResponse:
        """输入审核拒绝响应。

        【触发场景】
        - 输入审核判定为 should_refuse_medical_request
        - 用户输入包含违规内容（暴力、色情、违法等）

        【响应内容】
        - status: REFUSE
        - answer: 固定拒绝文案
        - risk_level: LOW
        - risk_flags: input_rejected:{verdict} + categories

        :param state: 状态总线
        :return: ConsultResponse（拒绝响应）
        """
        verdict = state.input_moderation.verdict if state.input_moderation else "Unsafe"
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.REFUSE,
            answer="很抱歉，该请求包含无法处理的内容，请重新描述问题。",
            risk_level=RiskLevel.LOW,
            risk_flags=[f"input_rejected:{verdict}"]
            + (state.input_moderation.categories if state.input_moderation else []),
        )

    async def _review(
        self, state: ConsultState, key, *, reason: str | None = None
    ) -> ConsultResponse:
        """审核不通过响应（保守处理）。

        【触发场景】
        - 输入审核解析失败（Guard 不可用）
        - 图片解析失败且无文字上下文
        - 输出审核 blocked

        【响应内容】
        - status: REVIEW
        - answer: 固定 fallback 文案
        - risk_level: MEDIUM（保守风险等级）
        - risk_flags: review:{violations} + degraded_services

        :param state: 状态总线
        :param key: 会话键
        :param reason: 审核不通过原因（可选）
        :return: ConsultResponse（审核响应）
        """
        violations = state.medical_review.violations if state.medical_review else []
        review_flags = ["review:" + v[:100] for v in violations[:3]]
        if reason:
            review_flags.append("review:" + reason)
        review_flags.extend(state.degraded_services)
        response = ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.REVIEW,
            answer=self._fallback_answer(),
            risk_level=RiskLevel.MEDIUM,
            risk_flags=list(dict.fromkeys(review_flags)) or ["review:content"],
            image_findings=state.vision_findings,
        )
        await self._save_turn(state, key, response)
        return response

    def _service_unavailable(self, state: ConsultState, message: str) -> ConsultResponse:
        """知识问诊服务不可用响应。

        【触发场景】
        - 生成模型不可用（KnowledgeConsultUnavailable）
        - 重试后仍然失败
        - 超时类故障（不重试）

        【响应内容】
        - status: ERROR
        - retryable: True（可重试）
        - error: KNOWLEDGE_CONSULT_UNAVAILABLE
        - knowledge_degraded: True（标记降级）

        :param state: 状态总线
        :param message: 错误消息
        :return: ConsultResponse（错误响应）
        """
        logger.warning("知识问诊不可用: %s", message, extra={"request_id": state.request_id})
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.ERROR,
            retryable=True,
            error=ErrorDetail(
                code="KNOWLEDGE_CONSULT_UNAVAILABLE",
                message="知识问诊服务暂时不可用，请稍后重试",
                retryable=True,
            ),
            risk_flags=list(dict.fromkeys(state.degraded_services)),
            knowledge_degraded=True,
        )

    def _fixed_urgent_response(self, state: ConsultState) -> ConsultResponse:
        """固定急症模板响应。

        【触发场景】
        - 文字急症预判命中（且无幂等键）
        - 风险分级为 EMERGENCY
        - 任何异常下且是急症

        【响应内容】
        - status: SUCCESS
        - answer_mode: 急症模式
        - answer: 固定急症文案（来自 medical_safety_service）
        - vet_recommendation: 急诊建议

        :param state: 状态总线
        :return: ConsultResponse（急症响应）
        """
        g = self.medical_safety_service.build_fixed_urgent_answer(state)
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.SUCCESS,
            answer_mode=g.answer_mode,
            answer=self._render(g),
            summary=g.summary,
            what_to_do_now=g.what_to_do_now,
            avoid_actions=g.avoid_actions,
            what_to_monitor=g.what_to_monitor,
            risk_level=g.risk_level,
            risk_flags=self._risk_flags(state),
            vet_recommendation=g.vet_recommendation,
            image_findings=state.vision_findings,
            follow_up_questions=[],
            disclaimer=g.disclaimer,
        )

    def _fixed_pet_ambiguous_response(self, state: ConsultState) -> ConsultResponse:
        """多宠歧义 → 固定友好追问（2026-08-19，不依赖生成模型）。

        直接列出宠物名字让用户确认，措辞自然；用户下一轮回答名字后走正常问诊。
        """
        names = "、".join(
            p.display_name for p in state.pets if p.name
        ) or f"{len(state.pets)} 只宠物"
        questions = [f"请问您说的是哪一只宠物呢？（{names}）"]
        answer = (
            f"我看到您的宠物档案里有 {names}，为了给您更准确的建议，"
            f"请先告诉我您这次问的是哪一只哦～"
        )
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.SUCCESS,
            answer_mode=AnswerMode.PROVISIONAL,
            answer=answer,
            summary=answer,
            possible_explanations=[],
            what_to_do_now=[],
            avoid_actions=[],
            what_to_monitor=[],
            risk_level=RiskLevel.LOW,
            risk_flags=self._risk_flags(state),
            vet_recommendation=VetRecommendation(
                recommended=False, urgency=VetUrgency.NONE, reason=""
            ),
            image_findings=state.vision_findings,
            follow_up_questions=questions,
            disclaimer=DEFAULT_DISCLAIMER,
        )

    @staticmethod
    def _fixed_out_of_scope_response(state: ConsultState) -> ConsultResponse:
        """明确非宠物问诊问题：短路返回，不生成医疗建议或追问。"""
        answer = (
            "这个问题不属于宠物健康问诊范围。"
            "我可以帮助分析猫狗的症状、图片、日常护理和就医紧急程度。"
        )
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.SUCCESS,
            answer_mode=AnswerMode.NORMAL,
            answer=answer,
            summary=answer,
            possible_explanations=[],
            what_to_do_now=[],
            avoid_actions=[],
            what_to_monitor=[],
            risk_level=RiskLevel.LOW,
            risk_flags=["out_of_scope"],
            vet_recommendation=VetRecommendation(
                recommended=False,
                urgency=VetUrgency.NONE,
                reason="",
            ),
            image_findings=[],
            follow_up_questions=[],
            disclaimer="本服务仅提供宠物健康与护理相关信息。",
        )

    def _error(
        self, state: ConsultState, code: str, message: str, *, retryable: bool
    ) -> ConsultResponse:
        """错误响应构造器。

        【使用场景】
        - 超时错误（REQUEST_DEADLINE_EXCEEDED）
        - 外部服务超时（ExternalServiceTimeout）
        - 内部错误（INTERNAL_ERROR）
        - 会话冲突（ConversationConflictError）

        【响应内容】
        - status: ERROR
        - error: ErrorDetail(code, message, retryable)
        - risk_flags: degraded_services（降级标记）
        - knowledge_degraded: True 如果 knowledge_consult 降级

        :param state: 状态总线
        :param code: 错误码
        :param message: 错误消息
        :param retryable: 是否可重试
        :return: ConsultResponse（错误响应）
        """
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.ERROR,
            retryable=retryable,
            error=ErrorDetail(code=code, message=message, retryable=retryable),
            risk_flags=list(dict.fromkeys(state.degraded_services)),
            knowledge_degraded="knowledge_consult" in state.degraded_services,
        )

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _merge_questions(state: ConsultState, generated: list[str]) -> list[str]:
        """优先确定性缺失项；眼部场景不接受模型自行追加追问。"""
        merged: list[str] = []
        if state.completeness:
            merged.extend(state.completeness.questions)
        # 短症状追问已由规则按风险优先级挑选，避免模型再追加成问题清单。
        if (
            state.case_facts.domain != "eye"
            and (
                state.completeness is None
                or state.completeness.reason
                not in ("keyword_thin", "general_care")
            )
        ):
            merged.extend(generated)
        asked = set(state.case_facts.asked_questions)
        limit = (
            2
            if state.completeness
            and state.completeness.reason == "keyword_thin"
            else 3
        )
        return list(dict.fromkeys(m for m in merged if m and m not in asked))[:limit]

    @staticmethod
    def _apply_inferred_species(state: ConsultState) -> None:
        """从宠物档案、本轮文字或最近会话补全物种，供 RAG 和生成使用。

        【推断优先级】
        1. 当前文字（"猫"/"狗"/"犬"关键词）
        2. 历史宠物档案（最近一轮的 pet_info.species）
        3. 历史文字（最近一轮的 user_text 关键词）

        【物种规范化】
        - 使用 normalize_species() 统一物种格式
        - 支持别名：cat/猫/猫咪 → cat，dog/狗/犬 → dog

        【状态更新】
        - state.pet_info.species：推断的物种
        - state._species_source：物种来源（用于日志和调试）
          - "current_text"：从当前文字推断
          - "history_pet_info"：从历史档案推断
          - "history_text"：从历史文字推断
          - "pet_info"：用户明确指定
          - "unknown"：无法推断

        【使用场景】
        - RAG 检索：需要物种信息来过滤知识库
        - 生成回答：需要物种信息来生成针对性建议
        - 风险分级：不同物种的风险规则可能不同

        :param state: 状态总线
        """
        current_species = (
            normalize_species(state.pet_info.species) if state.pet_info else None
        )
        if state.pet_info and current_species:
            if state.pet_info.species != current_species:
                state.pet_info = state.pet_info.model_copy(
                    update={"species": current_species}
                )
            # run 入口可能已从当前文字补全物种；加载历史后再次调用时保留
            # 原始来源，避免日志把 current_text 错记为 pet_info。
            if getattr(state, "_species_source", None) not in {
                "current_text",
                "history_pet_info",
                "history_text",
            }:
                state._species_source = "pet_info"
            return

        def infer_text(text: str) -> str | None:
            has_cat = "猫" in text
            has_dog = "狗" in text or "犬" in text
            if has_cat == has_dog:
                return None
            return "cat" if has_cat else "dog"

        species = infer_text(state.text or "")
        source = "current_text"

        if species is None:
            for turn in reversed(state.history):
                history_species = normalize_species(
                    str((turn.pet_info or {}).get("species") or "")
                )
                if history_species:
                    species = history_species
                    source = "history_pet_info"
                    break
                history_species = infer_text(turn.user_text or "")
                if history_species:
                    species = history_species
                    source = "history_text"
                    break

        if species is None:
            state._species_source = "unknown"
            return
        if state.pet_info is None:
            state.pet_info = PetInfo(species=species)
        else:
            state.pet_info = state.pet_info.model_copy(update={"species": species})
        state._species_source = source

    @staticmethod
    def _combined_user_text(state: ConsultState) -> str:
        """最终风险只聚合用户原话，不使用历史助手回答。

        【聚合逻辑】
        1. 收集历史所有 user_text（跳过空值）
        2. 追加本轮 user_text
        3. 用"。"连接成完整文本

        【设计意图】
        - 风险评估只基于用户输入（不基于助手回答）
        - 避免助手的安全建议被误判为风险
        - 确保急症规则能匹配到完整上下文

        :param state: 状态总线
        :return: 聚合后的用户文本
        """
        texts = [turn.user_text.strip() for turn in state.history if turn.user_text.strip()]
        if state.text.strip():
            texts.append(state.text.strip())
        return "。".join(texts)

    def _precheck_urgent(self, state: ConsultState) -> bool:
        """判断是否命中文字急症预判。

        :param state: 状态总线
        :return: True 如果 force_urgent_guidance 为 True
        """
        return bool(
            state.text_emergency_precheck
            and state.text_emergency_precheck.force_urgent_guidance
        )

    def _risk_flags(self, state: ConsultState) -> list[str]:
        """收集所有风险标记（急症预判 + 风险分级 + 降级服务）。

        【收集来源】
        1. text_emergency_precheck.reasons（文字急症预判原因）
        2. emergency_result.reasons（综合急症评估原因）
        3. risk_result.reasons（风险分级原因）
        4. degraded_services（降级服务标记）

        :param state: 状态总线
        :return: 去重后的风险标记列表
        """
        flags: list[str] = []
        if state.text_emergency_precheck:
            flags.extend(state.text_emergency_precheck.reasons)
        if state.emergency_result:
            flags.extend(state.emergency_result.reasons)
        if state.risk_result:
            flags.extend(state.risk_result.reasons)
        flags.extend(state.degraded_services)  # 降级标记（如 vision/redis/knowledge_consult）
        return list(dict.fromkeys(flags))

    async def _save_turn(self, state: ConsultState, key, response: ConsultResponse) -> None:
        """保存对话轮次到 Redis（会话历史）。

        【核心职责】
        1. 构建 ConversationTurn 对象
        2. 记录关键元数据（风险等级、回答模式、紧急程度）
        3. 记录模型版本（用于升级后定位变化来源）
        4. 调用 conversation_service.save_turn() 写入 Redis

        【记录内容】
        - 用户输入：user_text, pet_info, image_findings
        - 助手输出：answer, follow_up_questions, answer_mode
        - 风险评估：risk_level, risk_flags, vet_urgency
        - 模型版本：vision_model, knowledge_provider, guard_model 等

        【使用场景】
        - 多轮对话：加载上下文（load_context）
        - 物种推断：从历史档案推断物种
        - 效果观测：分析回答质量和风险分布

        :param state: 状态总线
        :param key: 会话键（Redis 键名）
        :param response: 问诊响应
        """
        from app.schemas.conversation import ConversationTurn

        # 构建对话轮次记录
        turn = ConversationTurn(
            turn_id="",
            created_at=utc_now_iso(),
            user_text=state.text,
            pet_info=state.pet_info.model_dump() if state.pet_info else {},
            image_findings=state.vision_findings,
            risk_level=response.risk_level or RiskLevel.LOW,
            risk_flags=response.risk_flags,
            assistant_status=response.status.value,
            answer_mode=response.answer_mode,
            vet_urgency=(
                response.vet_recommendation.urgency
                if response.vet_recommendation else VetUrgency.NONE
            ),
            assistant_answer=response.answer or "",
            follow_up_questions=response.follow_up_questions,
            case_facts=state.case_facts.model_dump(exclude_none=True),
            # V1.1 P1-6：记录模型/组件版本（§29），升级后可定位变化来源
            model_versions={
                "vision_model": self.s.consult_vision_model_name,
                "knowledge_provider": self.s.knowledge_provider,
                "knowledge_model": self.s.knowledge_model,
                "guard_model": self.s.consult_guard_model_name,
                "guard_mode": self.s.guard_mode,
                "consult_prompt": "consult_answer_first_v2.4.0",
            },
        )
        await self.conversation_service.save_turn(key, turn)

    async def _idempotent_handoff(
        self, command: ConsultCommand, owner: str, request_hash: str, deadline
    ) -> ConsultResponse | None:
        """幂等握手（V1.1 P1-4：owner 原子占位 + 轮询至自己的 deadline）。

        【核心职责】
        1. 查询缓存（有结果 → 直接返回）
        2. 尝试占位（try_claim → 成功 → 返回 None，本请求继续执行）
        3. 轮询等待（已有请求在处理 → 轮询至自己的剩余预算）
        4. 超时处理（轮询超时 → 抛 ConversationConflictError）

        【幂等键设计】
        - 基于 conversation_id + text + pet_info + images 计算 SHA-256
        - 相同内容 → 相同幂等键 → 返回缓存结果
        - 不同内容 → 不同幂等键 → 独立执行

        【Owner 机制】
        - 使用 Redis SETNX 实现原子占位
        - 每个请求生成唯一 owner_token
        - 只有占位者才能写入结果
        - 失败时释放占位，允许其他请求接管

        【轮询策略】
        - 每 0.2 秒检查一次缓存
        - 持有者失败释放占位 → 其他请求可接管
        - 轮询超时 → 抛异常（不返回错误响应）

        :param command: 问诊命令
        :param owner: 当前请求的 owner_token
        :param request_hash: 请求指纹（SHA-256）
        :param deadline: 超时预算
        :return: ConsultResponse（缓存结果）或 None（本请求继续执行）
        """
        key = command.idempotency_key
        assert key is not None
        tenant, user = command.auth.tenant_id, command.auth.user_id

        # 第一步：查缓存
        cached = await self.idempotency_repo.get_result(tenant, user, key, request_hash)
        if cached:
            return ConsultResponse.model_validate_json(cached)
        
        # 第二步：尝试占位
        if await self.idempotency_repo.try_claim(
            tenant, user, key, request_hash, owner=owner
        ):
            return None

        # 第三步：轮询等待（已有请求在处理）
        wait_started = __import__("time").monotonic()
        logger.info(
            "idempotency_wait_started",
            extra={"request_id": command.request_id},
        )
        while deadline.has_remaining(0.2):
            cached = await self.idempotency_repo.get_result(
                tenant, user, key, request_hash
            )
            if cached:
                return ConsultResponse.model_validate_json(cached)
            # 持有者失败释放占位 → 接管继续执行
            if await self.idempotency_repo.try_claim(
                tenant, user, key, request_hash, owner=owner
            ):
                return None
            await asyncio.sleep(0.2)
        
        # 第四步：超时处理
        logger.warning(
            "idempotency_wait_timeout",
            extra={
                "request_id": command.request_id,
                "wait_ms": round((__import__("time").monotonic() - wait_started) * 1000),
            },
        )
        raise ConversationConflictError("相同请求正在处理中，请稍后重试")

    @staticmethod
    def _cacheable_status(response: ConsultResponse) -> bool:
        """只缓存确定性终态（V1.1 P1-4）：retryable 错误不缓存。

        【可缓存状态】
        - SUCCESS（成功）
        - REFUSE（拒绝）
        - REVIEW（审核）
        - ERROR 且 retryable=False（不可重试的错误）

        【不可缓存状态】
        - ERROR 且 retryable=True（可重试错误，应释放占位让重试重新执行）

        :param response: 问诊响应
        :return: True 如果可缓存，False 否则
        """
        return (
            response.status
            in (ConsultStatus.SUCCESS, ConsultStatus.REFUSE, ConsultStatus.REVIEW)
        ) or (response.status == ConsultStatus.ERROR and not response.retryable)

    @staticmethod
    def _idempotency_fingerprint(command: ConsultCommand) -> str:
        """计算请求指纹（用于幂等缓存键）。

        【指纹内容】
        - conversation_id：会话 ID
        - text：用户输入文字
        - pet_info：宠物信息（species, name, age 等）
        - images：图片 SHA-256 列表

        【计算方式】
        1. 构建 payload 字典
        2. JSON 序列化（sort_keys=True, separators=(",", ":")）
        3. SHA-256 哈希

        【设计意图】
        - 相同内容 → 相同指纹 → 返回缓存结果
        - 不同内容 → 不同指纹 → 独立执行
        - 使用 SHA-256 确保唯一性和不可逆性

        :param command: 问诊命令
        :return: SHA-256 指纹（64 位十六进制字符串）
        """
        payload = {
            "conversation_id": command.conversation_id,
            "text": command.text,
            "pet_info": command.pet_info.model_dump(mode="json") if command.pet_info else None,
            "images": [image.sha256 for image in command.images],
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _render(g) -> str:
        """把结构化结果渲染成自然、简洁的主人沟通文本。

        【渲染逻辑】
        1. summary：总结（优先显示）
        2. visible_findings：图片观察（补充 summary 没有覆盖的内容）
        3. possible_explanations：可能的病因
        4. what_to_do_now：当前可以做的处理
        5. what_to_monitor：需要留意的症状
        6. avoid_actions：不要做的操作
        7. vet_recommendation：就医建议（带紧急程度标签）
        8. disclaimer：免责声明

        【设计原则】
        - 自然口语化（避免机械复述）
        - 图片/事实只补充 summary 没有覆盖的内容
        - 每项限制条数（避免信息过载）
        - 就医建议带紧急程度标签（急诊/尽快/24 小时内等）

        :param g: GeneratedConsultation（结构化生成结果）
        :return: 渲染后的自然语言回答
        """

        def sentence(value: str) -> str:
            value = (value or "").strip().rstrip("。；;，,")
            return f"{value}。" if value else ""

        def joined(items: list[str], *, limit: int = 3) -> str:
            values = [str(item).strip().rstrip("。；;，,") for item in items if str(item).strip()]
            return "；".join(values[:limit])

        summary = sentence(g.summary)
        parts = [summary] if summary else []

        # 图片或明确事实只补充 summary 没有覆盖的内容，避免“已确认的情况”机械复述。
        findings = [
            item for item in g.visible_findings
            if item and re.sub(r"\s+", "", item) not in re.sub(r"\s+", "", g.summary or "")
        ]
        if findings:
            parts.append("从目前提供的信息看，" + sentence(joined(findings, limit=2)))
        if g.possible_explanations:
            parts.append(
                "常见可以从这几个方向考虑："
                + sentence(joined(g.possible_explanations, limit=4))
            )
        if g.what_to_do_now:
            parts.append("您现在可以先这样做：" + sentence(joined(g.what_to_do_now)))
        if g.what_to_monitor:
            parts.append("接下来重点留意：" + sentence(joined(g.what_to_monitor)))
        if g.avoid_actions:
            parts.append("暂时不要：" + sentence(joined(g.avoid_actions, limit=2)))
        vr = g.vet_recommendation
        if vr and vr.recommended:
            urgency_label = {
                VetUrgency.EMERGENCY: "立即急诊", VetUrgency.URGENT: "尽快就医",
                VetUrgency.WITHIN_24_HOURS: "24 小时内就医", VetUrgency.BOOK_VET: "建议预约就医",
                VetUrgency.MONITOR: "密切观察", VetUrgency.NONE: "",
            }.get(vr.urgency, "")
            prefix = f"{urgency_label}：" if urgency_label else ""
            parts.append(prefix + sentence(vr.reason))
        if g.disclaimer:
            parts.append(sentence(g.disclaimer))
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _fallback_answer() -> str:
        """审核/异常时的固定 fallback 文案。

        【使用场景】
        - 输入审核解析失败
        - 图片解析失败且无文字上下文
        - 输出审核 blocked

        【文案内容】
        保守建议：症状加重 → 尽快就医

        :return: fallback 文案
        """
        return (
            "如果出现明显异常或症状加重，请尽快带宠物到线下宠物医院检查。"
        )

    async def close(self) -> None:
        """关闭所有外部服务连接（资源清理）。
        
        【清理对象】
        - image_service：图片解析服务
        - moderation：审核服务
        - consultation_service：问诊生成服务
        - conversation_service：会话管理服务
        
        【调用时机】
        - 应用关闭时
        - 服务重启时
        """
        for svc in (
            self.image_service,
            self.moderation,
            self.consultation_service,
            self.conversation_service,
        ):
            close = getattr(svc, "close", None)
            if close is not None:
                await close()