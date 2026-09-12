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
# ---------------------------------------------------------------------------
# 【导入区 1/2】标准库
# 只依赖 Python 自带模块，承担异步并发、指纹计算、序列化与正则匹配四类职责。
# ---------------------------------------------------------------------------
# 必须放在文件最顶部（docstring 之后、其他 import 之前）：让所有注解延迟求值，
# 从而可以直接书写 `X | None`、`list[str]` 等新语法，并避免循环导入时的运行期解析。
from __future__ import annotations

import asyncio                                   # wait_for（总超时兜底）、sleep（幂等轮询）
import hashlib                                   # SHA-256：幂等指纹、图片去重摘要
import json                                      # 幂等指纹规范化序列化(sort_keys) 与缓存结果读写
import logging                                   # 结构化日志：pipeline_start / stage_done / pipeline_end
import re                                        # 越界问诊正则、渲染前空白归一化
from collections.abc import Awaitable, Callable  # 进度回调签名（异步可调用）
from typing import Any, TypeAlias                # Any 宽松标注；TypeAlias 声明进度回调别名

# ---------------------------------------------------------------------------
# 【导入区 2/2】应用内模块
# 排列顺序即“依赖方向”：Agent 组件 → 核心配置/常量/异常 → 仓储 → RAG
#                   → 安全能力 → Schemas（数据结构） → Services（业务能力） → Utils
# ---------------------------------------------------------------------------

# --- Agent 内部组件 ---
from app.agent.completeness_checker import CompletenessChecker   # 信息完整度评估（决定是否走 provisional）
from app.agent.state import ConsultState                         # 状态总线：贯穿全流程的中间结果载体

# --- 核心常量：回答模式 / 响应状态 / 风险等级 / 就医紧急度 / 免责声明 ---
from app.core.constants import (
    AnswerMode,          # 回答模式：NORMAL / PROVISIONAL / URGENT_GUIDANCE
    ConsultStatus,       # 响应状态：SUCCESS / REFUSE / REVIEW / ERROR
    DEFAULT_DISCLAIMER,  # 默认免责声明（生成结果缺失时兜底）
    RiskLevel,           # 风险等级：LOW / MEDIUM / HIGH / EMERGENCY
    VetUrgency,          # 就医紧急度：EMERGENCY / URGENT / WITHIN_24_HOURS / ...
)

# --- 配置与超时预算 ---
from app.core.config import Settings            # 全局配置（阈值、开关、模型名、各阶段超时）
from app.core.deadline import DeadlineFactory   # 绝对 deadline 工厂；各阶段通过 child(cap=...) 领子预算

# --- 受控异常：流程中所有“可预期失败”都在此收口，便于分类降级 ---
from app.core.exceptions import (
    ConversationConflictError,       # 会话并发冲突 / 等锁或等幂等占位超时
    ExternalServiceTimeout,          # 外部服务（Vision/RAG/生成）超时基类
    KnowledgeConsultUnavailable,     # 生成模型不可用（超时、鉴权、连接错误统一映射到这里）
    RedisUnavailable,                # Redis 不可用 → 幂等/会话锁/历史全部降级为“无状态单轮”
    RequestDeadlineExceeded,         # 总预算耗尽（也可能是外层 wait_for 触发）
    VisionOutputInvalid,             # Vision 返回内容无法解析为结构化观察
    VisionTimeout,                   # Vision 单阶段超时
    VisionUnavailable,               # Vision 服务不可用（OOM、连接失败等）
)

# --- 仓储层 ---
from app.repositories.idempotency_repository import IdempotencyRepository  # 幂等占位 / 取结果 / 释放占位

# --- RAG 检索能力 ---
from app.rag.models import RagDecisionStatus                      # 检索决策：SUFFICIENT 表示证据充足
from app.rag.retriever import ShadowRetriever, normalize_species   # 检索器 + 物种归一化（猫/犬 → cat/dog）
from app.rag.emergency_shadow import V14EmergencyShadowMatcher     # V1.4 急症影子匹配（只记日志，不参与决策）

# --- 安全能力：急症规则、输入审核、输出审核 ---
from app.safety.emergency_rules import EmergencyRuleEngine   # 急症规则：文字预判 + 综合风险分级
from app.safety.input_moderator import InputModerator        # 输入审核器（保留注入位，便于替换实现）
from app.safety.output_moderator import OutputModerator      # 输出审核器（同上，当前主链路走 moderation）

# --- 对外数据结构（请求 / 响应 / 错误 / 宠物档案） ---
from app.schemas.consult import ConsultCommand, ConsultResponse, VetRecommendation  # 入参、出参、就医建议
from app.schemas.common import ErrorDetail                                          # 错误码 / 消息 / 可重试标记
from app.schemas.pet import PetInfo                                                 # 宠物档案（物种、名字等）

# --- 业务服务 ---
from app.services.conversation_service import ConversationService      # 会话：历史加载、分布式锁、存轮次
from app.services.consultation_service import ConsultationService      # 生成：normal/provisional/urgent/rewrite
from app.services.dialogue_archive import DialogueArchive              # 对话存档（JSONL，供监控与分析）
from app.services.image_service import ImageService                    # 图片解析服务（VisionGateway 封装）
from app.services.medical_safety_service import MedicalSafetyService   # 医疗安全：审核 / 局部修复 / 固定模板
from app.services.moderation_service import ModerationService          # 审核统一入口 check_input / check_output

# --- 工具 ---
from app.utils.time import utc_now_iso   # UTC ISO8601 时间戳（存档记录与对话轮次的时间字段）

# 模块级 logger：全部日志均带 request_id 便于按请求串联排查
logger = logging.getLogger(__name__)


# ============================================================
# 模块级工具函数
# ============================================================
# 这里的函数不依赖 ConsultAgent 实例状态，因此放在模块级别：
#   1) 单元测试可直接导入，无需构造整个 Agent；
#   2) 避免把“与状态无关的纯逻辑”堆进类里，保持类职责清晰。

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

    【为什么用文案判定而不是新增异常子类】
    这些异常由 knowledge_consult 客户端抛出，上层已在多处按 KnowledgeConsultUnavailable
    统一捕获；新增子类会牵动所有调用方的 except 分支。客户端契约保证“超时”场景的
    错误文案一定包含“超时”二字（可由 scripts/verify_deepseek_contract.py 验证），
    因此以文案作为判定依据成本最低，且不与具体实现强耦合。

    【使用场景】
    在 _execute() 的生成阶段捕获 KnowledgeConsultUnavailable 后调用，
    决定是否进入一次性重试逻辑。

    :param exc: KnowledgeConsultUnavailable 异常对象
    :return: True 表示应该重试，False 表示不应该重试
    """
    # 反向判定：只有被明确标记为“超时”的故障才拒绝重试，其余一律允许一次重试。
    # 注意：这只是“允许重试”的其中一个条件，调用方还会叠加
    #       “未重试过（_generate_retried）+ 剩余预算充足（has_remaining）”两个前提。
    return "超时" not in str(exc)


# ============================================================
# 模块级常量定义
# ============================================================
# 这些常量在模块导入时一次性构建（正则提前 compile），避免每个请求重复编译。

# 【非宠物问诊识别模式】高精度规则匹配，宁可少拦不误拦。
# 覆盖范围：仅“天气 / 气温 / 是否下雨”这三类确定与宠物无关的句式。
# 匹配方式：由 _is_out_of_scope_query() 使用 fullmatch 全串匹配（而非 search），
#           要求整句完全符合模板才判越界，进一步压低误拦概率。
# 时间前缀（今天/明天/后天/现在/当地）与谓词后缀（怎么样/如何/预报/情况…）
# 均为可选，因此“天气”、“今天天气怎么样”、“会不会下雨”都能命中。
_OUT_OF_SCOPE_PATTERNS = (
    re.compile(r"(?:今天|明天|后天|现在|当地)?(?:的)?天气(?:怎么样|如何|预报|情况)?"),
    re.compile(r"(?:今天|明天|后天|现在|当地)?(?:的)?气温(?:多少|怎么样|如何)?"),
    re.compile(r"(?:今天|明天|后天|现在|当地)?(?:会不会|是否|有)?下雨"),
)

# 【宠物健康语境白名单】命中任意一词即判定“这是宠物相关提问”，直接放行。
# 词表按四类信号组织：
#   1) 物种名：“猫 / 狗 / 犬 / 宠物”
#   2) 症状：“呕吐/吐/腹泻/拉稀/便/尿/呼吸/咳嗽/喷嚏/发热/发烧/疼/痛/掉毛”
#   3) 护理与日常：“洗澡/喂/食欲/精神/皮肤/伤口/腿/眼/耳”
#   4) 医疗行为：“药 / 疫苗 / 驱虫”
# 设计取向：宁可少拦（放过边界问句，最多多走一次生成），绝不误拦
#           （例如“天气热狗狗一直喘”含“狗”，必须继续正常问诊）。
_PET_CONTEXT_TERMS = (
    "猫", "狗", "犬", "宠物", "洗澡", "喂", "食欲", "精神", "呕吐", "吐",
    "腹泻", "拉稀", "便", "尿", "皮肤", "掉毛", "伤口", "腿", "眼", "耳",
    "呼吸", "咳嗽", "喷嚏", "发热", "发烧", "疼", "痛", "药", "疫苗", "驱虫",
)

# 【进度回调函数类型别名】SSE 流式模式下由 API 层注入的回调：
#   入参 1：事件名（如 "input_reviewed" / "vision_completed" / "risk_assessed"）
#   入参 2：事件负载（可 JSON 序列化的 dict，部分事件带 response 快照）
#   返回值：Awaitable[None]，由 _emit_progress 负责 await
# 非流式模式下该参数为 None（见 _emit_progress 的空值短路，零开销）。
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

    【为什么必须是独立异常类】
    _generate_streamed 内部对“流式通道故障”会 catch Exception 并回退非流式生成，
    因此必须先 `except StreamSafetyAbort: raise` 把安全违例透传给上层，否则它会被
    误当成通道故障而静默重试 —— 那等于放行了已知的不安全内容。
    """

    # 不携带额外字段：仅作为“控制流信号”使用，调用方只关心异常类型，
    # 不依赖消息内容（消息仅用于日志排查）。


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

    【事件时序（正常情况下客户端会依次收到）】
    input_reviewed → vision_started/vision_completed（有图时）→ risk_assessed
    → answer_generated → medical_review_completed → output_review_completed
    → urgent_guidance（仅急症短路时，且此时会直接带上完整 response 快照）

    :param progress: 进度回调函数（SSE 推送用）
    :param event: 事件名称（如 "input_reviewed", "vision_completed"）
    :param data: 事件数据（字典，包含阶段详情）
    """
    # 短路 1：非流式模式（progress=None），批量与内部调用都走这条路径，零开销返回。
    if progress is None:
        return
    # 短路 2：推送失败（客户端断连、SSE 写入报错）只记警告，向上抛出任何异常都不允许。
    # 进度事件属于“锦上添花”的可观测性能力，绝不能因为它失败而让整个问诊请求失败。
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

    【调用关系】
    API 层 → run() / run_stream()
             └─ _run_impl()   总编排：状态初始化 → 急症预判 → 幂等 → 会话锁 → 主流程 → 缓存 → 存档
                   └─ _execute()   13 个阶段的状态机（真正干活的地方）
                         └─ _answer() / _refuse() / _review() / _error() / _service_unavailable()  终态出口
    【终态出口枚举】任何路径最终都收敛到以下 5 个构造器之一，保证响应格式一致：
    - _answer()              SUCCESS：正常生成或卡片直答
    - _fixed_urgent_response() SUCCESS：急症固定模板（不依赖模型）
    - _refuse()              REFUSE ：输入审核判定需拒绝
    - _review()              REVIEW ：Guard 不可用 / 输出被拦，转保守处理
    - _error() / _service_unavailable() ERROR：超时、并发冲突、生成不可用

    【性能约束】
    整个 _execute() 被外层 asyncio.wait_for 包住，总耗时严格受 command.total_timeout_seconds 限制；
    因此新增阶段时必须从 deadline 领子预算，不得直接 await 无超时的外部调用。
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
        # --- 必需依赖：全部在 Container 组装阶段传入，缺失会直接启动失败 ---
        self.s = settings                                        # 全局配置：超时、开关、模型名等，下文统一用 self.s 访问
        self.image_service = image_service                       # 图片解析（VisionGateway 封装），失败只降级不阻断
        self.moderation = moderation_service                     # 审核统一入口：check_input / check_output
        self.input_moderator = input_moderator                   # 输入审核器（保留注入位，便于替换实现）
        self.output_moderator = output_moderator                 # 输出审核器（同上）
        self.emergency_rules = emergency_rules                   # 急症规则引擎：文字预判 + 综合风险分级
        self.conversation_service = conversation_service         # 会话服务：历史加载 / 分布式锁 / 存轮次
        self.completeness_checker = completeness_checker         # 信息完整度评估（决定 normal 还是 provisional）
        self.risk_engine = risk_engine                           # 风险分级引擎：合并各来源信号后定级
        self.consultation_service = consultation_service         # 生成服务：normal/provisional/urgent/rewrite
        self.medical_safety_service = medical_safety_service     # 医疗安全：审核 / 局部修复 / 固定模板
        self.idempotency_repo = idempotency_repo                 # 幂等存储（Redis）：占位 / 取结果 / 释放
        self.deadline_factory = deadline_factory                 # 超时预算工厂：生成绝对 deadline 时间点

        # --- 可选依赖：未装配时对应能力整体关闭，主链路仍可降级运行 ---
        self.rag_retriever = rag_retriever                       # None → 跳过 RAG 检索与卡片直答
        self.rag_emergency_matcher = rag_emergency_matcher        # None → 跳过急症影子匹配（只影响观测）
        self.dialogue_archive = dialogue_archive                 # None → 不写 JSONL 存档
        self.dialogue_repo = dialogue_repo                       # None → 不写 PG 存档

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

        【与 run_stream 的唯一区别】
        只有 token_sink 一个参数：为 None 时生成阶段一次性拿到完整结果；
        非 None 时逐 token 转发给 SSE。除生成阶段外，两者代码路径完全一致。

        :param command: 问诊命令（包含文字、图片、宠物信息等）
        :param progress: 进度回调函数（SSE 推送用）
        :return: ConsultResponse（问诊结果）
        """
        # 透传 token_sink=None → 告诉下游“本次不要走流式生成分支”
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

        【注意】流式并不代表“更快”或“无超时”：
        流式生成仍由 _generate_streamed 内部逐个 deadline.child(cap=20.0) 控制，
        且增量安全审核一旦命中违规会立即中断并回退固定安全模板。

        :param command: 问诊命令
        :param progress: 进度回调函数
        :param token_sink: Token 接收器（SSE 推送用）
        :return: ConsultResponse（问诊结果）
        """
        # 透传 token_sink（非 None）→ 下游 _execute 选择 _generate_streamed 分支
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

        【为什么步骤必须按这个顺序】
        1) 状态与物种推断最先：后续每一步都依赖 state，且物种影响 RAG 过滤条件；
        2) 影子匹配次之：纯观测不参与决策，放前面不会超时挤占业务预算；
        3) 文字急症预判先于一切模型调用：保证“模型全挂也能识别急症”；
        4) 幂等检查先于会话锁：重复请求可直接命中缓存返回，无需抢锁；
        5) 会话锁先于主流程：保证同一会话的多轮上下文串行，不出现交叉写；
        6) 缓存与存档放在最后：无论成功或失败都要落缓存与埋点，
           因此统一放在“后置收尾”，而不分散在各个分支里。

        :param command: 问诊命令
        :param progress: 进度回调
        :param token_sink: Token 接收器（None=非流式，有值=流式）
        :return: ConsultResponse
        """
        # ==========================================================
        # 步骤 1：初始化状态总线（封装 command，不修改原始命令）
        # ==========================================================
        # ConsultState.from_command() 是“由命令派生状态”的工厂：
        # 后续所有阶段只读写 state，绝不回写 command，保证入参不被污染，
        # 使同一个 command 可以被安全重试或并行复用（幂等轮询场景）。
        state = ConsultState.from_command(command)

        # ==========================================================
        # 步骤 2：推断物种（首轮，历史尚未加载）
        # ==========================================================
        # 优先级：本轮文字（猫/狗/犬）→ 历史宠物档案 → 历史文字。
        # 必须早于幂等指纹与 RAG：物种会影响 RAG 过滤条件，
        # 也是后续风险规则与生成 prompt 的输入之一。
        # 注：_execute 阶段 2（加载历史后）会再调用一次，用于多轮继承物种；
        #     这里先调是为了让“无历史”的链路也尽早拿到物种。
        self._apply_inferred_species(state)

        # ==========================================================
        # 步骤 3：创建 deadline 总预算（全链路超时控制）
        # ==========================================================
        # after_seconds(n) 返回的是“绝对时间点”而非倒计时：从此刻起所有子阶段
        # 只能通过 child(cap=...) / require(cap=...) 从这份总预算里领取份额，
        # 预算只减不增 —— 这是“重试不得重新获得完整预算”的实现基础。
        deadline = self.deadline_factory.after_seconds(command.total_timeout_seconds)
        # 记录入口时刻，用于最终 total_ms 统计（写入存档供性能分析）。
        # 模块顶层未 import time，故此处用 __import__ 惰性获取，避免与上方依赖混淆。
        _run_t0 = __import__("time").monotonic()

        # ==========================================================
        # 步骤 4：RAG 急症影子匹配（只观测，不决策）
        # ==========================================================
        # 目的：让新一版急症规则（V1.4 shadow matcher）在真实流量上“陪跑”，
        #      把命中规则 id 与最高严重度写进日志，供离线评估准确率与召回。
        # 关键约束：结果完全不参与本次决策；matcher 为 None 时整段跳过，
        #         即未装配该能力时不影响任何业务行为。
        if self.rag_emergency_matcher is not None:
            try:
                emergency_shadow = self.rag_emergency_matcher.search(
                    state.text,
                    # 有物种时带上，便于影子规则按物种细分评估命中情况
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
                # 影子匹配异常绝不能影响真实分诊：吞掉异常只记警告。
                # 这是全文件唯一“出错后可以什么都不做”的 try/except。
                logger.warning(
                    "rag_emergency_shadow_failed",
                    extra={"request_id": state.request_id},
                    exc_info=True,
                )

        # ==========================================================
        # 步骤 5：文字急症预判（纯规则，不调任何外部服务）
        # ==========================================================
        # 这是全链路最靠前的业务判断，刻意排在所有模型依赖之前：
        # 即便 Vision / Guard / 生成模型全部不可用，急症也能被识别并给出固定指导。
        # 结果同时写入 state，供后续风险分级（作为 precheck 入参）与
        # 异常兜底（_precheck_urgent）复用 —— 即“算一次，多处引用”。
        #
        # 为何带幂等键时不在这里短路：
        #   幂等键意味着调用方要求“同一请求只处理一次”，必须让步骤 6 的
        #   _idempotent_handoff 先完成缓存查询/占位握手，否则同一急症请求
        #   会被重复处理。因此这里只记录预判结果，真正返回推迟到步骤 8。
        state.text_emergency_precheck = self.emergency_rules.precheck_text(
            text=state.text, pet_info=state.pet_info
        )
        # 只有 EMERGENCY 才短路；HIGH/MEDIUM 仍需走完整链路（生成针对性的紧急指导）
        precheck_emergency = state.text_emergency_precheck.level == RiskLevel.EMERGENCY

        # 快路径：急症 + 无幂等键 → 固定模板立即返回，不进入 RAG/生成/审核
        if precheck_emergency and not command.idempotency_key:
            # 急症模板由 medical_safety_service 预置，完全不依赖模型可用性
            urgent = self._fixed_urgent_response(state)
            # 通过 SSE 把完整急症响应推给客户端（preliminary=False 表示终态而非初步建议）
            await _emit_progress(
                progress,
                "urgent_guidance",
                preliminary=False,
                response=urgent.model_dump(mode="json"),
            )
            return urgent

        # ==========================================================
        # 步骤 6：幂等性检查（防止重复请求）
        # ==========================================================
        # 典型场景：用户不小心点了两次提交，或网络超时后客户端重发，
        #          都希望“只被处理一次”，后续重复请求直接拿第一次的结果。
        # 实现：对“请求内容”计算 SHA-256 指纹；指纹相同即视为同一请求。
        # 注：指纹不包含 idempotency_key 本身，因此调用方换个 key 重发同样内容
        #     仍会被识别为同一请求（去重能力更强）。
        owner = state.request_id                                # 抢占者令牌：只有占位成功的请求才能回写结果
        request_hash = self._idempotency_fingerprint(command)   # 内容指纹，幂等缓存键的组成部分
        # 是否成功抢到占位：只有 True 时步骤 10 才需要回写缓存
        idempotency_claimed = False
        # 未传幂等键 → 整段跳过（无状态调用场景不做去重，省一次 Redis 往返）
        if command.idempotency_key:
            try:
                # _idempotent_handoff 有三种结果：
                #   返回 ConsultResponse      → 命中缓存 / 等到别人写完 → 直接返回
                #   返回 None                → 本请求抢到占位，继续走主流程
                #   抛 ConversationConflictError → 轮询到超时仍无结果
                cached = await self._idempotent_handoff(
                    command, owner, request_hash, deadline
                )
                if cached is not None:
                    # 缓存命中 → 直接返回缓存结果：不再调模型、不重新写历史与存档
                    return cached
                idempotency_claimed = True
            except RedisUnavailable as exc:
                # P0-2：Redis 不可用 → 放弃幂等优化，降级为“普通请求”继续问诊。
                # 这是有意取舍：宁可重复生成，也不因缓存故障直接拒服务。
                state.degraded_services.append("redis")
                state.warnings.append(str(exc))

        # ==========================================================
        # 步骤 7：会话锁获取（防止同一会话并发请求）
        # ==========================================================
        # 为何需要锁：同一 conversation 的“加载历史 → 生成 → 写历史”如果并发执行，
        # 会出现上下文交叉覆盖（A、B 都看到旧历史，最后写入的一条历史丢失）。
        # key 是 Redis 会话键：既用于加锁，也是 _execute 加载/保存历史时使用的键。
        key = self.conversation_service.key(
            state.tenant_id, state.user_id, state.conversation_id
        )
        lock_held = False  # 标记本请求是否真的持有锁，决定 finally 是否需要释放
        try:
            # 急症请求跳过抢锁：它不需要读历史上下文也不写历史，
            # 抢锁只会拖慢急救响应的首字延迟。
            if not precheck_emergency:
                try:
                    # 非阻塞抢占：抢到返回 True；被其他请求占用返回 False
                    lock_held = await self.conversation_service.acquire_lock(
                        key, owner_token=owner
                    )
                    if not lock_held:
                        # 锁被占用 → 短时等待持有者释放（上限 conversation_lock_wait_seconds）
                        lock_wait_started = __import__("time").monotonic()
                        logger.info(
                            "conversation_lock_wait_started",
                            extra={"request_id": state.request_id},
                        )
                        await self.conversation_service.wait_for_lock(
                            key, owner_token=owner
                        )
                        lock_held = True
                        # 记录等待耗时：排查“同一会话连续提问变慢”的关键指标
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
                    # 等待超时 → 向上抛，由步骤 9 统一转成 CONVERSATION_CONFLICT 响应
                    logger.warning(
                        "conversation_lock_timeout",
                        extra={
                            "request_id": state.request_id,
                            "wait_seconds": self.s.conversation_lock_wait_seconds,
                        },
                    )
                    raise
                except RedisUnavailable as exc:
                    # Redis 不可用 → 拿不到锁也不阻断：按“无锁单轮”继续（可能丢上下文）
                    state.degraded_services.append("redis")
                    state.warnings.append(str(exc))
                    lock_held = False

            # ======================================================
            # 步骤 8：执行主流程（_execute）
            # ======================================================
            # 这里嵌套两层 try，职责严格分离：
            #   内层 try/finally —— 只负责“无论成功失败都释放锁”
            #   外层 try/except —— 步骤 9，把异常翻译成标准响应
            try:
                if precheck_emergency:
                    # 带幂等键的急症：走到这里说明占位已成功，直接给固定模板
                    # （与步骤 5 快路径的响应内容完全一致，只是到达路径不同）
                    response = self._fixed_urgent_response(state)
                    await _emit_progress(
                        progress,
                        "urgent_guidance",
                        preliminary=False,
                        response=response.model_dump(mode="json"),
                    )
                else:
                    # V1.1 P0-3：外层硬兜底，总请求不超绝对 deadline。
                    # _execute 内部各阶段虽各自领了子预算，但只要有阶段忘了传递或
                    # 出现不可取消的阻塞 IO，仍可能整体超时，因此再加一层 wait_for。
                    # timeout=deadline.require() 取的是“剩余预算”：若已耗尽会先抛异常。
                    response = await asyncio.wait_for(
                        self._execute(
                            state, key, deadline,
                            progress=progress, token_sink=token_sink,
                        ),
                        timeout=deadline.require(),
                    )
            except asyncio.TimeoutError:
                # wait_for 触发 → 统一转成业务异常，避免调用方感知 asyncio 细节
                raise RequestDeadlineExceeded("处理超时，请稍后重试") from None
            finally:
                # 释放会话锁（即使 _execute 抛异常也会执行）；
                # 释放失败不处理：锁带 TTL，会兜底自动过期。
                if lock_held:
                    try:
                        await self.conversation_service.release_lock(
                            key, owner_token=owner
                        )
                    except RedisUnavailable:
                        pass  # TTL 兜底自动过期
        # ==========================================================
        # 步骤 9：异常处理（超时/冲突/服务异常 → 急症优先返回固定模板）
        # ==========================================================
        # 四个分支按“从具体到宽泛”排列，保证异常不会被前面的宽泛分支误吞。
        # 共通原则：能判断出“用户是急症”时，无论出什么错都要给出急救指导，
        #          宁可丢上下文也不能丢救命信息。
        except ConversationConflictError as exc:
            # 会话冲突（并发请求/等锁超时）→ 返回可重试错误，不消耗生成资源
            logger.warning(
                "conversation_conflict",
                extra={"request_id": state.request_id, "reason": str(exc)[:120]},
            )
            response = self._error(
                state, "CONVERSATION_CONFLICT", "会话正在处理中，请稍后重试", retryable=True
            )
        except RequestDeadlineExceeded:
            # 总超时 → 急症返回固定模板，否则返回可重试错误
            if self._precheck_urgent(state):
                response = self._fixed_urgent_response(state)
            else:
                response = self._error(
                    state, "REQUEST_DEADLINE_EXCEEDED", "处理超时，请稍后重试",
                    retryable=True,
                )
        except ExternalServiceTimeout as exc:
            # 外部服务超时（Vision/RAG/生成模型等）：沿用异常自带错误码，保留现场信息
            logger.warning("外部服务超时: %s", exc)
            if self._precheck_urgent(state):
                response = self._fixed_urgent_response(state)
            else:
                response = self._error(
                    state, exc.code, str(exc), retryable=True
                )
        except Exception:  # noqa: BLE001 - 兜底：命中急症仍给固定模板
            # 未预期异常（代码缺陷/依赖库异常）→ 记完整堆栈，急症仍走固定模板
            logger.exception("consult_unhandled_error", extra={"request_id": state.request_id})
            if self._precheck_urgent(state):
                response = self._fixed_urgent_response(state)
            else:
                response = self._error(
                    state, "INTERNAL_ERROR", "服务内部错误，请稍后重试", retryable=True
                )

        # ==========================================================
        # 步骤 10：缓存结果（幂等键 + 可缓存状态）
        # ==========================================================
        # 只有“本请求真正抢到占位”时才回写，避免多个并发请求同时覆盖缓存。
        if command.idempotency_key and idempotency_claimed:
            if self._cacheable_status(response):
                try:
                    # 写入终态结果：后续相同指纹的请求会直接命中这里写下的响应
                    await self.idempotency_repo.store_result(
                        state.tenant_id, state.user_id, command.idempotency_key,
                        request_hash, owner,
                        response.model_dump_json(),
                    )
                except RedisUnavailable:
                    # 写缓存失败 → 释放占位，允许其他请求重新执行（否则占位会一直挂到 TTL）
                    await self.idempotency_repo.release_claim(
                        state.tenant_id, state.user_id, command.idempotency_key,
                        request_hash, owner,
                    )
            else:
                # P1-4：retryable 错误不缓存，释放占位让重试可重新执行
                # （若缓存了错误响应，客户端重试会永远拿到同一个失败结果）
                await self.idempotency_repo.release_claim(
                    state.tenant_id, state.user_id, command.idempotency_key,
                    request_hash, owner,
                )

        # ==========================================================
        # 步骤 11：对话存档（JSONL + PG 双写，失败不影响主链路）
        # ==========================================================
        # total_ms 是“从 run 入口到响应组装完成”的端到端耗时，
        # 与 _execute 内部的阶段耗时（state._steps）配合，可用于定位瓶颈在网关还是在模型。
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

        【阶段依赖关系（改代码前必读）】
        - 阶段 3（输入审核）是“闸门”：解析失败/需拒绝都会提前 return，后面阶段不再执行；
        - 阶段 5 产出的 state.vision_findings 同时喂给：阶段 8 的风险红旗、阶段 9 的生成，
          以及 _detect_species_conflict / _detect_no_pet 的图文一致性判断；
        - 阶段 6 产出的 state.rag_result / state.rag_evidence 同时喂给：完整度追问、
          卡片直答（_fast_answer）以及生成阶段的证据注入；
        - 阶段 7 产出的 state.completeness 决定生成走 normal 还是 provisional；
        - 阶段 8 产出的 state.risk_result 决定生成走 urgent_guidance / provisional / normal，
          其中 EMERGENCY 会在本阶段内直接短路返回；
        - 阶段 10/11 是输出侧双闸门：医疗安全审核 + Guard 输出审核，任一不过即降级。

        【返回值与副作用】
        - 返回值：ConsultResponse（终态）。
        - 副作用：写 state（中间结果）、写 Redis 会话历史（_save_turn）、
          写 JSONL/PG 存档（_archive_dialogue，由 _run_impl 步骤 11 统一触发）。
        - 本方法不向调用方抛业务异常：所有可预期故障都转成对应终态响应；
          只有“预算彻底耗尽”才会向上抛 RequestDeadlineExceeded（由 Vision 分支显式抛出）。

        :param state: 状态总线（读写中间结果）
        :param key: 会话键（Redis 键名）
        :param deadline: 超时预算对象
        :param progress: 进度回调
        :param token_sink: Token 接收器
        :return: ConsultResponse
        """
        # 局部导入 time 并使用短别名：本方法内有大量毫秒级打点，
        # 用 _time 前缀避免与局部变量/业务名冲突，同时减少书写噪音。
        import time as _time
        _t0 = _time.monotonic()                 # 本方法入口时刻，用于 pipeline_end 的 total_ms
        rid = state.request_id                  # 请求 ID：所有日志都带它，便于按请求串联排查
        has_img = bool(state.image_inputs)      # 是否带图：影响非宠物分流、Vision 阶段与完整度判断
        
        # 全链路步骤耗时采集（2026-08-20）：
        # 故意挂到 state 引用而非局部变量 —— _execute 内所有 return 路径
        # 都能被 _archive_dialogue 读到（局部变量在提前 return 时就不可见了）。
        # 最终写入 dialogue JSONL，用于回答“这次请求中间经过哪些步骤、每步耗时多少”，
        # 是排查性能瓶颈与阶段缺失的一手依据。
        steps: list[dict] = []
        state._steps = steps
        
        # ==========================================================
        # 阶段 1：文字急症预判（最前置，纯规则；v6.3 §4 步骤 3）
        # ==========================================================
        # run() 入口（步骤 5）通常已经算过并写入 state，因此这里的 is None 判断
        # 只在“_execute 被直接调用”时成立（单测、内部复用）。
        # 这是幂等设计：重复调用不产生副作用，只是复用同一份结果。
        if state.text_emergency_precheck is None:
            state.text_emergency_precheck = self.emergency_rules.precheck_text(
                text=state.text, pet_info=state.pet_info
            )

        # ==========================================================
        # 阶段 2：加载历史（Redis 不可用 → 无记忆单轮降级，v6.3 §28）
        # ==========================================================
        # 历史有三个用途：
        #   1) 物种继承（本轮没说猫/狗时，从上一轮推断）；
        #   2) 风险聚合（_combined_user_text 会把历史用户原话拼起来做急症匹配）；
        #   3) 完整度判断（避免重复追问用户已经回答过的问题）。
        # 降级策略：Redis 挂了就拿不到历史，但不报错 —— 退化为“无记忆单轮”，
        # 代价是可能重复追问，但服务仍可用。
        try:
            snapshot = await self.conversation_service.load_context(key)
            state.history = snapshot.turns        # 历史轮次列表（按时间升序）
            state.history_summary = snapshot.summary  # 超出窗口的早期对话摘要
        except RedisUnavailable as exc:
            # Redis 不可用 → 降级为无历史单轮对话（state.history 保持默认空列表）
            state.degraded_services.append("redis")
            state.warnings.append(str(exc))

        # 本轮没有物种时，从同一会话最近一轮的宠物档案或用户文字继承。
        # 注意：这是第二次调用（第一次在 _run_impl 步骤 2，那时还没有 history）。
        # 有了历史后，能覆盖“用户第一轮说“我家猫”，第二轮只说“它不吃东西””这类场景。
        self._apply_inferred_species(state)
        # pipeline_start：每个请求的第一条业务日志，记录输入概要与物种来源，
        # 用于事后回溯“这次到底带了什么输入进来”。
        logger.info(
            "pipeline_start",
            extra={
                "request_id": rid,
                "has_images": has_img,
                "text_len": len(state.text or ""),
                "text": (state.text or "")[:300],   # 截断到 300 字，避免日志体积失控
                "pet_species": state.pet_info.species if state.pet_info else None,
                # species_source 取值见 _apply_inferred_species 的说明（current_text 等）
                "species_source": getattr(state, "_species_source", None),
            },
        )

        # ==========================================================
        # 阶段 3：场景化输入审核（v6.3 §13.1.1：医疗求助不因 Violent 拒）
        # ==========================================================
        # 超时策略：guard_enforced 打开时，从总预算里再领一份 guard_input_timeout 上限；
        # guard_enforced 关闭时传 None（代表不限制）—— 主要用于本地调试或影子模式。
        guard_input_timeout = (
            deadline.require(cap=self.s.guard_input_timeout)
            if self.s.guard_enforced
            else None
        )
        _ti = _time.monotonic()   # 本阶段计时起点
        # 把用户原文字交给 Guard 审核；结果整体写入 state.input_moderation，
        # 供后续判定（parse_ok / should_refuse_medical_request）与风险标记复用。
        state.input_moderation = await self.moderation.check_input(
            state.text,
            timeout_seconds=guard_input_timeout,
            request_id=state.request_id,
        )
        logger.info("stage_done", extra={"request_id": rid, "stage": "input_moderation", "ms": round((_time.monotonic() - _ti) * 1000)})
        steps.append({"stage": "input_moderation", "ms": round((_time.monotonic() - _ti) * 1000)})
        if state.input_moderation.parse_ok is False:
            # V1.1 P1-1：Guard 不可用/解析失败 → 保守 review（§28 不静默放行）；
            # 急症仍优先返回固定急症模板。
            # 设计要点：审核链路“宁严不漏” —— 审核不可用时不得当成“没问题”放行，
            # 而是转人工/保守回答，同时保住急症通道。
            if self._precheck_urgent(state):
                return self._fixed_urgent_response(state)
            return await self._review(state, key)
        if state.input_moderation.should_refuse_medical_request:
            # 审核判定“这条请求不应回答”（如与宠物医疗无关的违规内容）
            logger.info("request_refused", extra={"request_id": rid, "reason": "input_moderation"})
            return self._refuse(state)
        # 审核通过 → 推送 SSE 事件，“输入已过审”属于客户端可展示的第一个进度点
        await _emit_progress(progress, "input_reviewed")

        # ==========================================================
        # 阶段 4：明确的非宠物问诊请求直接分流
        # ==========================================================
        # 目的：避免“今天天气怎么样”这类问题进入 RAG、医疗追问和生成链路，
        #      既省成本，也避免模型硬挖出一个宠物病因。
        # 策略：仅做高精度规则匹配；含宠物/症状上下文的问题（如“天气热狗狗喘”）仍正常问诊。
        if self._is_out_of_scope_query(state.text, has_images=has_img):
            response = self._fixed_out_of_scope_response(state)
            # 本阶段无外部调用，耗时恒为 0，写入 steps 是为了保持阶段序列完整可读
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
            # 仍需写入会话历史：下一轮用户说“那我家猫呢”时能接上上下文
            await self._save_turn(state, key, response)
            return response

        # ==========================================================
        # 阶段 5：图片解析（经 VisionGateway；V1.1 P0-3：阶段共享预算 15s）
        # ==========================================================
        # 用视觉模型分析图片，提取物种猜测、观察项（observation）与红旗信号（red_flags）。
        # 核心容错原则：图片失败绝不阻断问诊，只做三件事——
        #   1) 在 state.degraded_services 记上 "vision"；
        #   2) 在 state.warnings 留下原因；
        #   3) 把 vision_findings 清空，避免下游误用旧值。
        # 最终回答会因此被标为 provisional（见阶段 7）。
        if state.image_inputs:
            # 先推“开始”事件：图片推理可能排队等待，客户端需要即时反馈
            await _emit_progress(
                progress,
                "vision_started",
                image_count=len(state.image_inputs),
            )
            vision_telemetry: list[dict] = []        # 网关回传的逐图遥测（缓存命中/排队/推理耗时）
            vision_degraded_reason: str | None = None  # 降级原因，仅用于 SSE 事件回传
            _tv = _time.monotonic()                  # 本阶段计时起点
            try:
                # deadline.child(cap=...) 给 Vision 单独的 15s 子预算：
                # 即使 Vision 超时，也只烧掉这 15s，不会把总预算一次性耗光。
                state.vision_findings = await self.image_service.analyze(
                    state.image_inputs,
                    state.text or None,   # 文字作为辅助上下文一起送进去，提升识别准确率
                    deadline=deadline.child(cap=self.s.vision_timeout_seconds),
                    request_id=state.request_id,
                    telemetry=vision_telemetry,
                )
                logger.info("stage_done", extra={"request_id": rid, "stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "findings": len(state.vision_findings)})
                steps.append({"stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "findings": len(state.vision_findings)})
            except (VisionTimeout, VisionUnavailable, VisionOutputInvalid) as exc:
                # 三类可预期故障统一降级：超时 / 服务不可用 / 输出无法解析。
                # vision_degraded_reason 拼上异常类名，便于在 SSE 事件里区分原因。
                vision_degraded_reason = f"{type(exc).__name__}: {str(exc)[:120]}"
                state.degraded_services.append("vision")
                state.warnings.append(str(exc))
                state.vision_findings = []   # 必须清空：防止上一次调用残留的半成品结果被下游使用
                logger.warning("stage_failed", extra={"request_id": rid, "stage": "vision", "error": str(exc)[:120]})
                steps.append({"stage": "vision", "ms": round((_time.monotonic() - _tv) * 1000), "degraded": True, "error": str(exc)[:120]})
            except RequestDeadlineExceeded as exc:
                # 视觉网关抛出的 RequestDeadlineExceeded 说的是“Vision 自己的子预算耗尽了”，
                # 并不等于整个请求已超时，所以要分两种情况处理：
                #   总预算还有剩 → 弃图保文，继续往下走（常见：Vision 排队过久）；
                #   总预算也没了 → 无法继续任何阶段，直接向上抛（由 _run_impl 步骤 9 收口）。
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
            # vision_completed 事件把网关遥测（缓存命中/排队/推理/格式重试）一并回传，
            # 让客户端与运维无需查日志就能看到“这 15s 到底花在哪”。
            # max(...) 取的是本次多图请求中最差的那一张，代表真实用户体验上限。
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

        # vision_failed 的三个条件必须同时成立才算“图片真的没用上”：
        #   1) 用户确实传了图；2) 没有任何观察结果；3) 确实因降级导致的空结果。
        # 三个条件缺一不可，避免把“Vision 正常返回但图里什么都没有”误判为服务故障。
        vision_failed = bool(
            state.image_inputs
            and not state.vision_findings
            and "vision" in state.degraded_services
        )
        # 是否存在可用的文字上下文（本轮文字或历史里的用户原话）
        has_text_context = bool(
            state.text.strip() or any(t.user_text.strip() for t in state.history)
        )
        if vision_failed and not has_text_context:
            # 只有图片、且图片服务不可用 → 没有任何可供生成的事实，
            # 禁止调用模型凭空猜测，转保守 review（reason 会写进 risk_flags 便于观测）。
            return await self._review(state, key, reason="image_unavailable")

        # ==========================================================
        # 阶段 5.5：图片异常检测（v1.2 §4.6：图文物种冲突 / 无宠物）
        # ==========================================================
        # 只做判断不修改状态；具体的处置（清空 vision_findings、追加追问）
        # 统一放在阶段 7 的完整度处理里，因为那一步需要同时兼顾追问与风险。
        species_conflict = self._detect_species_conflict(state)
        no_pet = self._detect_no_pet(state)

        # ==========================================================
        # 阶段 6：RAG 检索（意图判断 + 追问查缺 + grounded 证据）
        # ==========================================================
        # 一次检索同时完成三件事，避免重复走知识库：
        #   1) decision/reason_codes —— 判断“知识库能不能回答这个问题”；
        #   2) questions_to_ask       —— 卡片自带的追问清单（用于查缺）；
        #   3) build_grounded_evidence —— 带出处的事实证据，注入生成 prompt。
        # 容错：检索器为 None（未装配）或抛异常都不阻断问诊，
        #      只是退化为“无知识库增强”的纯生成路径。
        rag_questions: list[str] = []   # 从命中的 top 卡片提取出的追问问题（最多 3 条）
        _trg = _time.monotonic()        # 本阶段计时起点
        if self.rag_retriever is not None:
            try:
                state.rag_result = self.rag_retriever.search(
                    state.text,
                    # 物种作为过滤条件：避免把“猫”的知识卡片推荐给“狗”的问诊
                    species=state.pet_info.species if state.pet_info else None,
                )
                if self.s.rag_grounded:
                    # 开关控制：把检索结果转成“带出处的事实条目”，
                    # 供生成阶段引用，减少模型自行编造病因。
                    state.rag_evidence = self.rag_retriever.build_grounded_evidence(
                        state.rag_result
                    )
                if (
                    self.s.rag_followup_check
                    # 只有“证据充足”时才拿卡片追问清单：证据不足时追问应由
                    # 完整度检查器按缺失字段给出，而不是照搬卡片问卷。
                    and state.rag_result.decision is RagDecisionStatus.SUFFICIENT
                    and state.rag_result.hits
                ):
                    top_card = self._top_card(state.rag_result)   # 相似度最高的卡片
                    rag_questions = [
                        str(q) for q in top_card.get("questions_to_ask", []) if q
                    ][:3]   # 限 3 条，避免一次抛给用户过多问题
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
                # 检索是“增强项”而非必需项：没它也能生成，只是质量略降
                logger.warning("rag_failed", extra={"request_id": rid}, exc_info=True)
        # 不论成功/失败/未启用都记录一条 steps，保证阶段序列在监控里完整可见
        steps.append({"stage": "rag", "ms": round((_time.monotonic() - _trg) * 1000), "hits": len(state.rag_result.hits) if state.rag_result else 0})

        # ==========================================================
        # 阶段 7：完整度判断（带卡片追问查缺；不阻断回答，只决定 provisional）
        # ==========================================================
        # 评估用户提供的信息是否充足，产出 state.completeness：
        #   need_more_info —— 是否信息不足（决定生成走 normal 还是 provisional）
        #   reason         —— 不足的类型（hard_need / keyword_thin / pet_ambiguous …）
        #                    下游多个分支依赖这个值做差异化处理，新增取值需同步排查
        #   questions      —— 待向用户追问的问题列表
        # 关键设计：信息不足不停服务，只降级为“初步建议 + 追问”。
        state.completeness = self.completeness_checker.evaluate(
            state, rag_questions=rag_questions
        )
        if vision_failed:
            # 用户明确上传了图片但本轮未能观察，回答必须标注为 provisional。
            # dict.fromkeys(...) 用于去重并保持原顺序，避免重复追问。
            state.completeness.need_more_info = True
            state.completeness.questions = list(
                dict.fromkeys(
                    state.completeness.questions
                    + ["本次未能解析图片，请重新上传清晰图片或补充文字描述。"]
                )
            )
        if species_conflict:
            # 图文物种冲突：以文字为准，追问确认前不采信图片观察。
            # 处置理由：宁可少用一次图片信息，也不能在“猫狗搞错”的前提下给建议。
            state.completeness.need_more_info = True
            conflict_msg = (
                "您上传的照片中看到的宠物与您描述的不一致，请确认照片是否为同一只宠物，"
                "或重新上传对应的照片。"
            )
            # 冲突追问插到最前面：这是必须优先澄清的前提
            state.completeness.questions = list(
                dict.fromkeys([conflict_msg] + state.completeness.questions)
            )
            state.warnings.append("species_conflict")
            # 冲突未确认前以文字为准：图片观察不参与风险与生成
            state.vision_findings = []
        elif no_pet and not has_text_context:
            # 图里没宠物且没有文字：没有任何事实依据，只给“重新拍图”的唯一追问
            state.completeness.need_more_info = True
            state.completeness.questions = [
                "这张照片里没有识别到宠物，请重新拍摄您的宠物照片，或用文字描述情况。"
            ]
        elif no_pet:
            # 有文字：忽略图片，继续文字问诊（降级标记 vision_no_pet 便于统计）
            state.degraded_services.append("vision_no_pet")
            state.warnings.append("图片中未识别到宠物，已按文字描述回答")
            state.vision_findings = []

        # ==========================================================
        # 阶段 8：最终风险分级（合并文字预判 + 图片红旗 + 档案）
        # ==========================================================
        # 分两步评估，职责不同，不能互相替代：
        #   emergency_rules.evaluate → 只看“有没有急症信号”（急诊语义）
        #   risk_engine.evaluate     → 综合定级 LOW/MEDIUM/HIGH/EMERGENCY（分级语义）
        state.emergency_result = self.emergency_rules.evaluate(
            # 风险只看用户原话：不把历史助手回答拼进去，
            # 否则“建议尽快就医”这类助手文案会被误判成用户描述的症状
            text=self._combined_user_text(state),
            # 把每张图的红旗信号展平成单个列表传入。
            # 注：内层循环变量复用了 f（实际遍历的是 f.red_flags），效果是展平，
            #     语义正确但可读性差，改动时请保留“展平”这一行为。
            red_flags=[f for f in state.vision_findings for f in f.red_flags],
            pet_info=state.pet_info,
            # 复用阶段 1 预判结果，避免重复跑一遍规则（也保证两处结论一致）
            precheck=state.text_emergency_precheck,
        )
        _tr = _time.monotonic()   # 本阶段计时起点（只计 risk_engine 部分）
        state.risk_result = self.risk_engine.evaluate(state)
        logger.info("stage_done", extra={"request_id": rid, "stage": "risk_assess", "ms": round((_time.monotonic() - _tr) * 1000), "risk_level": state.risk_result.level.value, "urgent": state.emergency_result.force_urgent_guidance})
        steps.append({"stage": "risk_assess", "ms": round((_time.monotonic() - _tr) * 1000), "risk_level": state.risk_result.level.value})
        await _emit_progress(progress, "risk_assessed")
        if state.risk_result.level == RiskLevel.EMERGENCY:
            # 最终定级为 EMERGENCY → 短路返回固定急症模板。
            # 注意：这里走的是“全量信息判定后”的急症，与 _run_impl 步骤 5 的
            # “纯文字预判急症”是两条不同的入口，共用同一个模板构造函数。
            urgent = self._fixed_urgent_response(state)
            await _emit_progress(
                progress,
                "urgent_guidance",
                preliminary=False,
                response=urgent.model_dump(mode="json"),
            )
            # 急症也要存历史：用户下一轮说“已经到医院了”时才能接上上下文
            await self._save_turn(state, key, urgent)
            return urgent

        # ==========================================================
        # 6.5 直答通道（v1.2 §4.3：简单问答卡片直答，不调生成模型）
        # ==========================================================
        # 适用场景：“猫能不能吃巧克力”这类事实性简单问题，
        #          知识卡片里已有现成答案，直接渲染卡片即可，省一次模型推理。
        # 五个条件全部满足才会走直答（任一不满足则继续往下走生成链路）：
        #   1) 开关打开；2) 检索器已装配；3) 本轮真有检索结果；
        #   4) 风险为 LOW/MEDIUM（高风险必须走生成，不能拿卡片搪塞）；
        #   5) 检索器自判“可直接回答”（置信度超阈）。
        # 2026-08-19：信息不足（need_more_info）时不直答，走追问（§4.7 查缺闭环）——
        #             因为此时卡片答案可能答非所问，先问清楚再说。
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
            # 注意：_fast_answer 内部仍会走医疗安全审核 + 输出审核，
            # 直答“省模型”但不省安全闸门。
            fast = await self._fast_answer(state, key, progress=progress)
            logger.info("pipeline_end", extra={"request_id": rid, "status": "fast_answer", "answer_mode": "normal", "risk_level": state.risk_result.level.value, "total_ms": round((_time.monotonic() - _t0) * 1000), "answer_len": len(fast.answer or "")})
            return fast

        # ==========================================================
        # 6.6 多宠歧义固定追问（2026-08-19）
        # ==========================================================
        # 不确定用户在问哪一只时，直接返回友好的固定追问模板，不调生成模型。
        # 原因：模型容易输出“存在对象混淆风险，请明确宠物”这类生硬措辞。
        # 下一轮用户回答名字后，走正常问诊流程。
        if (
            state.completeness is not None
            and state.completeness.need_more_info
            and state.completeness.reason == "pet_ambiguous"
        ):
            resp = self._fixed_pet_ambiguous_response(state)
            logger.info("pipeline_end", extra={"request_id": rid, "status": "success", "answer_mode": resp.answer_mode.value, "risk_level": state.risk_result.level.value, "total_ms": round((_time.monotonic() - _t0) * 1000), "answer_len": len(resp.answer or ""), "pet_ambiguous": True})
            return resp

        # ==========================================================
        # 阶段 9：生成回答（三模式；EMERGENCY 已在上方固定短路）
        # ==========================================================
        # 根据风险等级与信息完整度选模式，优先级从高到低：
        #   1) 流式（token_sink 非 None）—— 只要开了流式就用流式，与风险无关；
        #   2) HIGH                        —— urgent_guidance（紧急指导，不短路）
        #   3) 信息不足（hard_need/keyword_thin）—— provisional（初步建议 + 追问）
        #   4) 其他                        —— normal（完整回答）
        # 注意此处用 cap=20.0 独立给生成阶段一份上限 20s 的子预算：
        # 生成往往是最耗时的阶段，固定上限可避免它把总预算全部吃光，
        # 给后面的医疗审核/输出审核留出时间。
        _tg = _time.monotonic()   # 本阶段计时起点
        try:
            if token_sink is not None:
                # 流式模式：逐 token 生成，实时推送（返回的是模式字符串）
                mode = await self._generate_streamed(
                    state, deadline, token_sink,
                    request_id=rid,
                )
            elif state.risk_result.level == RiskLevel.HIGH:
                # 高风险模式：生成紧急指导（不短路，仍走生成流程），
                # 保证能带上“马上就医 + 路上注意什么”这类针对性内容
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
            # 流式增量审核命中违规：立即切换固定安全模板，
            # 防止模型继续输出不安全的医疗建议。
            # 注：此分支不赋值 mode（模式是流式内部的局部概念），
            #     而 mode 在 except 之后不再被引用，因此不会触发 UnboundLocalError。
            state.degraded_services.append("safety_stream_abort")
            state.warnings.append(str(exc))
            state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
            logger.warning(
                "stage_failed",
                extra={"request_id": rid, "stage": "generate", "error": "stream_safety_abort"},
            )
        except KnowledgeConsultUnavailable as exc:
            # 本地 9B 偶发输出解析失败：在 deadline 内重试一次（2026-08-18）。
            # 但模型超时通常表示容量已经饱和，此时立即重试会形成重试风暴。
            # 三个条件同时成立才重试（与 _should_retry_knowledge_failure 配合）：
            #   1) 非超时类故障（超时意味着模型已饱和，重试只会雪上加霜）；
            #   2) 本次请求还没重试过（getattr 兼容 state 上无此属性的情况）；
            #   3) 剩余预算 ≥ 12s（否则重试必然又被 deadline 砍断，浪费一次调用）。
            if (
                _should_retry_knowledge_failure(exc)
                and not getattr(state, "_generate_retried", False)
                and deadline.has_remaining(12.0)
            ):
                # 满足重试条件：非超时类故障 + 未重试过 + 剩余预算充足
                # 先置位再重试：即使重试途中再次进入此分支，也只会走 else 返回错误
                state._generate_retried = True
                logger.warning(
                    "stage_generate_retry",
                    extra={"request_id": rid, "error": str(exc)[:120]},
                )
                try:
                    # 重试时的模式选择与主分支严格一致：
                    # 否则会出现“首次走 provisional、重试走 normal”的语义漂移。
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
                    # 阶段名用 generate_retry，便于在监控里把“重试成功”与“首次成功”区分开
                    steps.append({"stage": "generate_retry", "mode": mode, "ms": round((_time.monotonic() - _tg) * 1000)})
                except KnowledgeConsultUnavailable as exc2:
                    # 重试仍然失败 → 返回服务不可用（错误响应会被 _run_impl 标记为可重试，
                    # 因此步骤 10 不会缓存它，客户端重试能真正重新执行）
                    state.degraded_services.append("knowledge_consult")
                    state.warnings.append(str(exc2))
                    logger.warning(
                        "stage_failed",
                        extra={"request_id": rid, "stage": "generate", "error": str(exc2)[:120]},
                    )
                    return self._service_unavailable(state, str(exc2))
            else:
                # 不满足重试条件：超时类故障或预算不足 → 直接返回错误。
                # retry_skipped 字段帮助运维快速区分“为啥没重试”：
                #   True  → 因为判定为超时（饱和），主动跳过；
                #   False → 非超时故障，只是预算不够了。
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
        # answer_generated 事件不带回答内容，只带耗时：具体文本由流式 token 事件负责
        await _emit_progress(progress, "answer_generated", ms=generate_ms)

        # ==========================================================
        # 阶段 10：医疗安全后置检查 + 受 deadline 限制的重写一次（v6.3 §16.2）
        # ==========================================================
        # 这是回答侧的“安全漏斗”，共四级，逐级降级（从好到保险）：
        #   ① review()          规则审核    —— 过则直接用（绝大多数情况）
        #   ② repair_locally()  确定性修复  —— 只改违规字段，保留正确内容
        #   ③ rewrite_once()    模型重写    —— 限时一次；重写后再跑一次修复
        #   ④ build_fixed_safe_answer() —— 固定安全模板（最终兜底）
        _tm = _time.monotonic()   # 本阶段计时起点（包含四级降级的全部耗时）
        state.medical_review = await self.medical_safety_service.review(
            state.generated,
            # 同上：把各图红旗展平后传入，供审核规则判定“高风险回答是否充分提醒就医”
            red_flags=[f for f in state.vision_findings for f in f.red_flags],
            expected_risk=state.risk_result.level,        # 回答的风险定级需与评估结果一致
            expected_urgency=state.risk_result.vet_urgency,  # 就医紧急度也需一致
        )
        # ② 可确定性修复的规则问题只修改违规字段，保留症状、图片观察和护理建议。
        # 这一步避免“重写超时 → 整段固定模板”吞掉原本正确的针对性回答。
        # 关键点：repair_locally 返回 None 表示“无法确定性地修”，
        #          此时保留原文不覆盖，继续走第 ③ 级。
        if not state.medical_review.passed:
            repaired = self.medical_safety_service.repair_locally(
                state.generated,
                violations=state.medical_review.violations,
                expected_risk=state.risk_result.level,
                expected_urgency=state.risk_result.vet_urgency,
            )
            if repaired is not None:
                state.generated = repaired
                # 修复后必须重新审核：修复结果本身也可能不达标
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
        # ③ 模型重写：只在“还有时间”时才尝试，避免为了重写把整个请求拖超时。
        # deadline.has_remaining(_rewrite_cap) 是硬门槛：剩余预算不足一个重写窗口
        # 就干脆不试，直接进第 ④ 级固定模板（省一次注定失败的模型调用）。
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
                    # 重写后仍不过 → 再试一次确定性修复（重写可能只解决了一部分违规）
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
                # 重写环节任何异常（超时、模型报错）都不向外抛：
                # 下面紧跟的第 ④ 级会兜住，保证一定有一份安全的回答返回给用户。
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
        # ④ 最终兜底：前面的三级都没能通过审核 → 整段换成预置的安全模板。
        # 这是“医疗安全不容协”的底线：宁可回答变得笼统，也不能输出违规内容。
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
        # 告知客户端“安全审核已结束”：流式场景下它是在全部 token 发完之后才到
        await _emit_progress(
            progress,
            "medical_review_completed",
            ms=medical_review_ms,
        )

        # ==========================================================
        # 阶段 10.5：无证据时禁止编造病因（确定性收口）
        # ==========================================================
        # 场景：RAG 无可用证据且信息不足时，模型偶尔仍会在正文或就医理由里
        # 自行枚举具体疾病/专科方向。
        # 处置：清空 answer_text，让响应基于收口后的结构化字段重新渲染，
        # 避免模型正文残留未经证据支持的病因（详细触发条件见方法本身 docstring）。
        if self._apply_provisional_no_evidence_guard(state):
            logger.info(
                "provisional_no_evidence_guard",
                extra={
                    "request_id": rid,
                    "rag_decision": state.rag_result.decision.value,
                    "reason_codes": state.rag_result.reason_codes,
                },
            )

        # 提取 RAG 检索结果的分类信息（供后续输出过滤使用）。
        # 用途：clean_owner_facing_language 需要知道本次回答落在哪些知识分类上，
        #      才能判断哪些专业术语可以保留、哪些要改写成主人能听懂的话。
        # 用 dict.fromkeys 去重同时保序，保证结果稳定可比。
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
        # 例如：去掉“建议转诊眼科”等不适合宠物主人的表述。
        # 返回值：(改写后的 GeneratedConsultation, 是否发生了改动)，
        # 用第二个返回值决定要不要打日志（避免无意义日志刷屏）。
        # is_eye_case 单独传：眼部场景有专属的措辞改写规则。
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

        # ==========================================================
        # 阶段 11：输出通用审核（场景化；受同一 deadline 约束）
        # ==========================================================
        # 用 Guard 审核“即将发给用户的回答文本”（state.generated），
        # 检查是否包含违规或不安全内容。这是回答侧的第二道闸门：
        #   阶段 10 医疗安全 = 领域规则（是否充分提醒就医、有无违规用药建议）
        #   阶段 11 输出审核 = 通用内容安全（政治、暴力、色情等）
        _to = _time.monotonic()   # 本阶段计时起点
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
            # 输出被审核拦截 → 进入人工审核流程（不把被拦内容发给用户，
            # 但仍保存这一轮历史，避免用户下一轮失去上下文）
            return await self._review(state, key)
        output_moderation_ms = round((_time.monotonic() - _to) * 1000)
        await _emit_progress(
            progress,
            "output_review_completed",
            ms=output_moderation_ms,
        )

        # ==========================================================
        # 阶段 12：组装响应 + 存历史
        # ==========================================================
        # _answer 负责：计算最终风险等级、渲染回答文本、合并追问、
        # 写入 Redis 会话历史，并返回 status=SUCCESS 的 ConsultResponse。
        response = await self._answer(state, key)
        # pipeline_end：每个请求的收尾日志，记录状态/模式/风险/耗时/回答长度与预览，
        # 是排查“这次请求到底给了用户什么”的最后一道信息源。
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

        【“两段式”指什么】
        适配器以 (kind, value) 二元组的方式回传：
        - kind == "token" → 增量文本片段（边生成边推送）；
        - 其他 kind      → 结构化结果（完整 GeneratedConsultation 对象），
                            赋值给 state.generated，供后续审核/渲染使用。
        因此是“先流文本、再拿结构体”，两者不能互相替代。

        【为何必须做增量审核而不能等生成完】
        流式已经把文字发给客户端了，事后再审核已经来不及。所以每累积 60 字符
        就拼上句尾声明跑一次规则审核，一旦命中立即中断并降级为固定安全模板。
        60 是延迟与成本的折中：字太少会频繁匹配，字太多则风险暴露窗口过大。

        :param state: 状态总线
        :param deadline: 超时预算
        :param token_sink: Token 接收器（SSE 推送用）
        :param request_id: 请求 ID
        :return: 生成模式（urgent_guidance/provisional/normal）
        """
        # 延迟导入：诊断规则只在流式路径需要，顶层导入会拉长模块加载时间
        from app.safety.diagnosis_rules import DiagnosisRules

        # 模式选择规则与非流式分支保持一致（HIGH 优先于信息不足），
        # 同时把对应的生成方法绑定到 generate，供“流式失败回退”时直接复用。
        if state.risk_result.level == RiskLevel.HIGH:
            mode = "urgent_guidance"
            generate = self.consultation_service.generate_urgent_guidance
        elif (
            state.completeness.need_more_info
            and state.completeness.reason
            # 注：这里比 _execute 的非流式分支多了一个 pet_ambiguous。
            # 实际上 6.6 已在更早的位置短路了 pet_ambiguous，走到这里说明
            # 该分支属于防御性兼容，不会影响正常流程。
            in ("hard_need", "pet_ambiguous", "keyword_thin")
        ):
            mode = "provisional"
            generate = self.consultation_service.generate_provisional
        else:
            mode = "normal"
            generate = self.consultation_service.generate
        # 构建咨询请求：把 state（症状/历史/证据/完整度等）与模式打包成适配器入参
        request = self.consultation_service.knowledge_consult.build_request(state, mode)
        # 取底层适配器：真正的流式能力在 adapter 上，而非 knowledge_consult 门面
        adapter = self.consultation_service.knowledge_consult.adapter
        answer_parts: list[str] = []   # 已推送的文本片段（用于拼出完整回答）
        checked_until = 0              # 已审核到的字符位置（避免重复审核同一段文字）
        try:
            # 流式生成：逐块消费适配器回传的 (kind, value)
            async for kind, value in adapter.generate_consultation_stream(
                request=request,
                # 流式同样受 20s 子预算约束，不会因“边生成边推送”而失去超时保护
                deadline=deadline.child(cap=20.0),
                request_id=request_id,
            ):
                if kind == "token":
                    text = str(value)
                    answer_parts.append(text)
                    # 立即转发给客户端：这是流式体验的核心（用户马上看到字）
                    await token_sink(text)
                    joined = "".join(answer_parts)
                    if len(joined) - checked_until >= 60:
                        checked_until = len(joined)
                        # 拼上句尾声明后再审核：模拟“完整回答”的形态，
                        # 避免因为缺少结尾声明而被规则误判（反之也可能漏判）
                        if DiagnosisRules.violations(joined + "。本回答仅供参考。"):
                            # 命中违规 → 立即中断整个生成，不继续输出任何字
                            raise StreamSafetyAbort("流式增量审核命中违规") from None
                else:
                    # 非 token 事件 → 结构化结果，直接接管为本次生成产物
                    state.generated = value
        except StreamSafetyAbort:
            # 安全违例必须原样上抛：不能被下面的宽泛 except 当成“通道故障”
            # 而回退非流式重生成，那等于把已知的不安全内容再跑一遍。
            raise
        except Exception as exc:  # noqa: BLE001 - 流式失败回退非流式
            # 通道故障（适配器报错、SSE 断连等）→ 退化为一次性非流式生成，
            # 保证“流式不可用”不会变成“问诊不可用”。
            logger.warning(
                "stream_failed_fallback_non_stream",
                extra={"request_id": request_id, "error": str(exc)[:160]},
            )
            state.generated = await generate(state, deadline=deadline.child(cap=20.0))
            return mode
        # 正常情况下用累积的 token 拼出完整回答；空串时不覆盖
        # （state.generated.answer_text 可能是适配器给的更优版本）。
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

        【调用前提（由调用方保证）】
        rag_retriever 与 rag_result 均非空，且已通过 is_fast_answerable 判定。
        本方法用 assert 显式声明这一契约：若被绕过调用，会在开发期直接报错，
        而不是在生产上静默产生空回答。

        :param state: 状态总线
        :param key: 会话键
        :param progress: 进度回调
        :return: ConsultResponse（直答结果）
        """
        # 局部导入：这两个符号只有直答路径使用，放顶层会污染模块命名空间
        from app.core.constants import AnswerMode
        from app.schemas.consult import GeneratedConsultation, VetRecommendation

        # 契约断言：保护后续 payload[...] 取值不会因 None 而崩溃
        assert self.rag_retriever is not None and state.rag_result is not None
        # 把卡片内容按物种渲染成结构化字段（summary / 处理建议 / 追问等）
        payload = self.rag_retriever.build_fast_answer(
            state.rag_result,
            # 传归一化后的物种（cat/dog），让卡片能选用对应的物种表述
            query_species=(
                normalize_species(state.pet_info.species) if state.pet_info else None
            ),
        )
        # 构建直答结果：answer_mode 固定 NORMAL（直答不属于初步建议或紧急指导）；
        # vet_recommendation 固定“不推荐就医”：
        # 高风险/需就医的情况已在前面被拦截，走不到直答。
        # self_reported_confidence=None：直答不经过模型，没有模型自评置信度。
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
        # 走医疗检查：虽不调生成模型，但仍走同一套规则审核（毫秒级，几乎无开销）。
        # red_flags 传空列表：直答只在 LOW/MEDIUM 且信息充足时触发，
        # 高危红旗信号不可能出现在这条路径上。
        state.medical_review = await self.medical_safety_service.review(
            state.generated,
            red_flags=[],
            expected_risk=state.risk_result.level,
            expected_urgency=state.risk_result.vet_urgency,
        )
        if not state.medical_review.passed:
            # 直答不重写（没有模型可重写）→ 直接降级为固定安全模板
            state.generated = self.medical_safety_service.build_fixed_safe_answer(state)
        # 与主链路一致地推送进度事件，保证客户端的事件序列不因走直答而缺环
        await _emit_progress(progress, "answer_generated")
        await _emit_progress(progress, "medical_review_completed")
        # 注：此处 timeout_seconds=None（不做超时限制），
        #     因为直答本身几乎瞬时完成，不需要从总预算里再切一块出来。
        state.output_moderation = await self.moderation.check_output(
            state.generated,
            timeout_seconds=None,
            request_id=state.request_id,
        )
        if state.output_moderation.blocked:
            # 输出被拦 → 统一转保守 review（与主链路行为一致）
            return await self._review(state, key)
        await _emit_progress(progress, "output_review_completed")
        # 复用 _answer 完成渲染与存历史，避免直答与正常回答出现两套组装逻辑
        return await self._answer(state, key)

    def _top_card(self, result) -> dict:
        """取检索结果中相似度最高（hits[0]）的知识卡片原始字典。

        【用途】
        只有这里需要卡片的原始字段（如 questions_to_ask 追问清单），
        其他流程一律使用检索器封装好的结构化结果。

        【为何每次重建索引】
        report.cards 是列表结构，按 id 建字典是为了 O(1) 查找；
        卡片数量有限，且只在“证据充足”时调用，构造成本可忽略。

        :param result: 检索结果（RagResult）
        :return: 卡片字典；无命中或检索器未装配时返回空字典 {}
        """
        # 三重防御：无结果 / 无命中 / 检索器未装配 → 一律返回空字典。
        # 调用方用 top_card.get("questions_to_ask", []) 取值，不会因缺键报错。
        if not result or not result.hits or self.rag_retriever is None:
            return {}
        # 卡片列表 → 按 id 索引，避免线性扫描
        cards = {c["id"]: c for c in self.rag_retriever.report.cards}
        # hits[0] 即相似度最高的那张卡；取不到时返回空字典
        return cards.get(result.hits[0].card_id, {})

    def _detect_species_conflict(self, state: ConsultState) -> bool:
        """图文物种冲突：图片观察到的物种与文字/档案物种不一致（v1.2 §4.6）。

        仅对真实 Vision 输出生效（mock 观察不参与冲突判定）。

        【判定思路】
        只有“文字明确说了猫或狗”且“图片识别出了具体的猫/狗但两者不同”才算冲突。
        任一侧模糊（比如文字只写“宠物”、图片 species_guess 不在 cat/dog 内）
        都不判冲突，避免误报导致图片信息被白白丢弃。

        :param state: 状态总线
        :return: True 表示存在图文物种冲突
        """
        # mock_vision 模式下图片观察是造出来的，不参与冲突判定；
        # 无图片观察或无宠物档案时也无法比较
        if self.s.mock_vision or not state.vision_findings or not state.pet_info:
            return False
        # 把文字侧的物种名归一到 dog/cat；无法归一（没提物种）则直接排除冲突
        text_species = (state.pet_info.species or "").strip()
        if "狗" in text_species or "犬" in text_species:
            text_norm = "dog"
        elif "猫" in text_species:
            text_norm = "cat"
        else:
            return False
        # 收集图片侧所有明确的猫/狗猜测（unknown 等模糊值不计入）
        vision_species = {
            f.species_guess for f in state.vision_findings
            if f.species_guess in ("cat", "dog")
        }
        # 有明确图片猜测、且文字物种不在其中 → 判为冲突
        return bool(vision_species and text_norm not in vision_species)

    @staticmethod
    def _detect_no_pet(state: ConsultState) -> bool:
        """图片中没有宠物：物种 unknown 且无任何观察（v1.2 §4.6）。

        【为何要求两个条件同时成立】
        只看 species_guess == "unknown" 会把“识别出物种但没注意到异常”的正常照片
        误判为“没有宠物”。加上“且无任何观察”后，语义才是“完全没看出有宠物”。

        :param state: 状态总线
        :return: True 表示图片里没识别到宠物
        """
        # 任意一张图满足“物种未知 + 无观察”即认为没拍到宠物
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
        # —— 以下 6 个前置条件必须全部成立才收口（任一不满足即不干预）——
        # ① 生成结果与检索结果都存在
        if generated is None or rag_result is None:
            return False
        # ② 必须是“初步建议”模式（正常/紧急指导由各自的安全策略负责）
        if generated.answer_mode is not AnswerMode.PROVISIONAL:
            return False
        # ③ 检索证据不足（证据充足时允许模型基于证据具体分析）
        if rag_result.decision is RagDecisionStatus.SUFFICIENT:
            return False
        # ④ 原因码必须是“模糊泛指查询”，而不是“缺少某个必需字段”
        #    （后者应通过追问补齐，而不是抹掉病因说明）
        if "vague_general_query" not in rag_result.reason_codes:
            return False
        # ⑤ 没有 grounded 证据
        if state.rag_evidence:
            return False
        # 图片已经给出可用观察时，不得用通用“状态不佳”覆盖视觉结论。
        if state.vision_findings:
            return False

        # —— 收口动作：用保守表述覆盖可能与事实不符的字段 ——
        species = normalize_species(state.pet_info.species if state.pet_info else None)
        # 按物种选择称呼（狗/猫/统一谓“宠物”），让文案更自然
        subject = {"dog": "狗狗", "cat": "猫咪"}.get(species, "宠物")
        generated.summary = (
            f"{subject}目前状态不佳，但现有信息有限，暂时无法判断具体原因。"
            "请继续观察并补充症状持续时间、精神食欲和活动情况。"
        )
        # 清空“可能病因”：这是本次收口的核心目的（防止无证据的病因猜测）
        generated.possible_explanations = []
        # 就医理由也改成不含具体病因的通用表述
        generated.vet_recommendation.reason = (
            "由于目前信息有限，暂时无法判断具体原因。若状态持续、加重或出现其他异常，"
            "建议及时就医检查。"
        )
        # 清空模型正文：强制 _answer 走 _render() 用上面收口后的结构化字段重新拼装，
        # 否则模型原文里残留的具体病名仍会被直接返回给用户。
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
        # 有图片 → 永远不算越界：图片里可能就有宠物，不能凭文字否定
        if has_images:
            return False
        # 归一化：去空白与标点并转小写，让“今天天气怎么样？”与“今天天气怎么样”等价
        normalized = re.sub(r"[\s，。！？、,.!?]+", "", (text or "").lower())
        # 空文本不判越界；命中任一宠物语境词也不判越界（宁可少拦）
        if not normalized or any(term in normalized for term in _PET_CONTEXT_TERMS):
            return False
        # 最后才用 fullmatch 全串匹配：必须整句完全符合模板才判越界
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
        # 两个写端都没装配 → 整个存档功能关闭，直接返回（零开销）
        if (self.dialogue_archive is None or not self.dialogue_archive.enabled) and self.dialogue_repo is None:
            return
        
        # 构建完整的对话记录（包含所有中间状态）
        record = {

                # ① 标识与时间：能把一条记录准确定位到具体请求/用户/会话
                "ts": utc_now_iso(),
                "request_id": state.request_id,
                "tenant_id": state.tenant_id,
                "user_id": state.user_id,
                "conversation_id": state.conversation_id,
                # ② 输入快照：用户原话、宠物档案、图片观察（便于复盘 bad case）
                "user_text": state.text,
                "pet_info": state.pet_info.model_dump() if state.pet_info else None,
                "image_findings": [f.model_dump(mode="json") for f in state.vision_findings],
                # ③ 响应结果：状态/模式/风险/标记（用于统计分布与降级率）
                "status": response.status.value,
                "answer_mode": response.answer_mode.value if response.answer_mode else None,
                "risk_level": response.risk_level.value if response.risk_level else None,
                "risk_flags": response.risk_flags,
                # ④ 检索与降级信息：命中卡片/决策/得分，用于评估 RAG 效果
                "hit_card_ids": (
                    [h.card_id for h in state.rag_result.hits] if state.rag_result else []
                ),
                "rag_decision": state.rag_result.decision.value if state.rag_result else "",
                "rag_top_score": state.rag_result.top_score if state.rag_result else None,
                "degraded_services": state.degraded_services,
                # ⑤ 输出内容：追问与最终回答全文，供人工抽检
                "follow_up_questions": response.follow_up_questions,
                "answer": response.answer,
                # ⑥ 性能数据：端到端耗时 + 各阶段耗时明细
                "total_ms": total_ms,
                # 全链路步骤耗时（2026-08-20）：input_moderation/vision/rag/risk_assess/
                # generate/generate_retry/medical_review/output_moderation
                # 用 getattr 容错：若存档在非 _execute 路径被调用，_steps 可能不存在
                "steps": [dict(s) for s in getattr(state, "_steps", [])],
            }
        # 双写：JSONL 供实时监控/离线分析，PG 供 SQL 查询与统计。
        # 两个写端各自判断启用状态，互不影响（任一端挂了不会影响另一端）。
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
        # 备注：上层（_run_impl 步骤 11 / 各短路分支）已保证此处 g 一定非空；
        # assert 是开发期契约检查，生产环境用 -O 运行时会被移除，零开销。
        g = state.generated
        assert g is not None
        # 最终风险等级取“阶段 8 评估值”与“生成结果自报值”的较大者。
        # 为什么取 max：模型可能在回答中识别出更严重的信号（如图片里更明显的异常），
        # 不能因为阶段 8 定级偏低就把它压下去 —— 风险宁可高估不可低估。
        level = self.risk_engine.max_level(state.risk_result.level, g.risk_level)
        response = ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.SUCCESS,
            answer_mode=g.answer_mode,
            # 回答渲染优先级：优先用模型生成的完整正文 answer_text；
            # 为空时（例如被 _apply_provisional_no_evidence_guard 清空）
            # 才回退到 _render() 用结构化字段拼装。
            answer=(g.answer_text or self._render(g)),
            summary=g.summary,
            possible_explanations=g.possible_explanations,
            what_to_do_now=g.what_to_do_now,
            avoid_actions=g.avoid_actions,
            what_to_monitor=g.what_to_monitor,
            risk_level=level,
            # 风险标记汇总：预判原因 + 综合评估原因 + 风险分级原因 + 降级服务
            risk_flags=self._risk_flags(state),
            vet_recommendation=g.vet_recommendation,
            image_findings=state.vision_findings,
            # 追问合并：确定性缺失项优先，并过滤已问过的问题（详见 _merge_questions）
            follow_up_questions=self._merge_questions(state, g.follow_up_questions),
            self_reported_confidence=g.self_reported_confidence,
            # 免责声明兜底：模型没给就用默认文案，保证响应结构始终完整
            disclaimer=g.disclaimer or DEFAULT_DISCLAIMER,
            # knowledge_degraded 让前端知道“本次回答没有知识库增强”
            knowledge_degraded="knowledge_consult" in state.degraded_services,
        )
        # 存历史：让下一轮能拿到本轮问答上下文（也是物种继承的数据来源）
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
        # 审核对象缺失（极端异常路径）时按 "Unsafe" 兜底：拒绝分支宁严勿宽
        verdict = state.input_moderation.verdict if state.input_moderation else "Unsafe"
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            # ① 状态固定 REFUSE：前端据此展示“无法处理”，不渲染任何医疗内容
            status=ConsultStatus.REFUSE,
            # ② 固定拒绝文案：不引用、不回显用户输入，避免违规内容二次传播
            answer="很抱歉，该请求包含无法处理的内容，请重新描述问题。",
            # ③ 被拒绝不等于存在健康风险，风险等级固定 LOW
            risk_level=RiskLevel.LOW,
            # ④ 风险标记 = 审核判定结论 + 命中的违规分类（供风控侧统计口径）
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
        # 违规明细兜底为空列表：Guard 不可用时 medical_review 可能为 None
        violations = state.medical_review.violations if state.medical_review else []
        # 每条截断到 100 字符、最多取前 3 条：risk_flags 只承担“标记”职责，
        # 不宜把整段违规文本塞进响应体（既冗余又可能二次回显敏感内容）
        review_flags = ["review:" + v[:100] for v in violations[:3]]
        # 追加调用方传入的原因（如 image_unavailable），用于定位是哪条分支转的 review
        if reason:
            review_flags.append("review:" + reason)
        # 叠加降级服务标记：让“为何转人工”的完整原因链可查
        review_flags.extend(state.degraded_services)
        response = ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.REVIEW,
            # 固定保守文案：审核不通过时绝不透传未经审核的模型输出
            answer=self._fallback_answer(),
            # 取 MEDIUM 作为“未知风险”的保守档（既不谎报急症，也不放行）
            risk_level=RiskLevel.MEDIUM,
            # dict.fromkeys 去重保序；全部为空时兜底 "review:content"，
            # 保证 risk_flags 永不为空，否则前端无从解释这条 REVIEW 因何而来
            risk_flags=list(dict.fromkeys(review_flags)) or ["review:content"],
            # 保留图片观察结果：转人工复核时人工需要看到原始发现
            image_findings=state.vision_findings,
        )
        # 转人工同样要落历史，保证下一轮上下文里存在“曾建议人工审核”这一事实
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
        # 先落一条 warning：服务不可用属于必须被监控捕获的事件（error 级可能过于噪）
        logger.warning("知识问诊不可用: %s", message, extra={"request_id": state.request_id})
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            # ERROR 而非 REVIEW：这是“系统没算出来”，语义上不是“内容不安全”
            status=ConsultStatus.ERROR,
            retryable=True,
            # 对外文案固定，不透传底层异常 message（可能含内部实现细节）
            error=ErrorDetail(
                code="KNOWLEDGE_CONSULT_UNAVAILABLE",
                message="知识问诊服务暂时不可用，请稍后重试",
                retryable=True,
            ),
            # 降级服务去重后写入 risk_flags，便于统计“模型不可用”发生频次
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
        # 文案来自 medical_safety_service：急症话术属于医疗安全资产，
        # 必须与安全规则同源维护，不能散落在这个 Agent 里各写一份
        g = self.medical_safety_service.build_fixed_urgent_answer(state)
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            # 急症必须“正常返回”：转 REVIEW/ERROR 会丢失“立即就医”这个唯一行动指令
            status=ConsultStatus.SUCCESS,
            answer_mode=g.answer_mode,
            # 模板通常不提供 answer_text，因此统一走 _render 结构化拼装
            answer=self._render(g),
            summary=g.summary,
            what_to_do_now=g.what_to_do_now,
            avoid_actions=g.avoid_actions,
            what_to_monitor=g.what_to_monitor,
            risk_level=g.risk_level,
            risk_flags=self._risk_flags(state),
            vet_recommendation=g.vet_recommendation,
            image_findings=state.vision_findings,
            # 急症场景不追问：任何追问都会稀释并延迟“立刻就医”这一指令
            follow_up_questions=[],
            disclaimer=g.disclaimer,
        )

    def _fixed_pet_ambiguous_response(self, state: ConsultState) -> ConsultResponse:
        """多宠歧义 → 固定友好追问（2026-08-19，不依赖生成模型）。

        直接列出宠物名字让用户确认，措辞自然；用户下一轮回答名字后走正常问诊。
        """
        # 用宠物显示名拼出可读列表；display_name 为空时退化为“N 只宠物”，
        # 保证文案里永远有可展示内容（不会出现空括号这种尴尬输出）
        names = "、".join(
            p.display_name for p in state.pets if p.name
        ) or f"{len(state.pets)} 只宠物"
        # 只问一条：先确定“问的是哪只”，否则后续所有症状建议都缺少主体
        questions = [f"请问您说的是哪一只宠物呢？（{names}）"]
        # answer/summary 共用同一段话：歧义场景下没有可单独摘录的要点
        answer = (
            f"我看到您的宠物档案里有 {names}，为了给您更准确的建议，"
            f"请先告诉我您这次问的是哪一只哦～"
        )
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            # 交互层面是成功的（已给出可执行的追问），故不是 REVIEW/ERROR
            status=ConsultStatus.SUCCESS,
            # PROVISIONAL：尚无实质医疗建议，仅完成“主体确认”这一步
            answer_mode=AnswerMode.PROVISIONAL,
            answer=answer,
            summary=answer,
            possible_explanations=[],
            what_to_do_now=[],
            avoid_actions=[],
            what_to_monitor=[],
            # 歧义本身不代表健康风险，取 LOW
            risk_level=RiskLevel.LOW,
            risk_flags=self._risk_flags(state),
            # 主体未确认前不给就医建议：避免把 A 猫的急诊信号套到 B 狗身上
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
        # 文案包含两层信息：明确拒答边界 + 说明本服务能做什么（引导用户改问法）
        answer = (
            "这个问题不属于宠物健康问诊范围。"
            "我可以帮助分析猫狗的症状、图片、日常护理和就医紧急程度。"
        )
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            # 非宠物问题是“正常业务范围内的拒答”，用 SUCCESS 而非 ERROR，
            # 否则会污染错误率指标、并触发前端重试逻辑
            status=ConsultStatus.SUCCESS,
            answer_mode=AnswerMode.NORMAL,
            answer=answer,
            summary=answer,
            # 其余结构化字段全部置空：本分支不产出任何医疗结论
            possible_explanations=[],
            what_to_do_now=[],
            avoid_actions=[],
            what_to_monitor=[],
            risk_level=RiskLevel.LOW,
            # 固定标记 out_of_scope，供统计“越界提问”占比
            risk_flags=["out_of_scope"],
            vet_recommendation=VetRecommendation(
                recommended=False,
                urgency=VetUrgency.NONE,
                reason="",
            ),
            # 即便用户传了图也不回传观察结果：避免诱导用户把本服务当通用识图工具
            image_findings=[],
            follow_up_questions=[],
            # 免责声明换成“仅提供宠物健康信息”，与拒答语义保持一致
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
        # 统一错误出口：所有异常分支都经此构造响应，保证 error 字段结构一致。
        # 注意 retryable 同时出现在顶层与 ErrorDetail 内，二者必须同值
        # （顶层供网关/客户端决策，内层供业务侧读取）。
        return ConsultResponse(
            request_id=state.request_id,
            conversation_id=state.conversation_id,
            status=ConsultStatus.ERROR,
            retryable=retryable,
            error=ErrorDetail(code=code, message=message, retryable=retryable),
            # 保留降级痕迹：即使最终报错，也要能看出此前已经降级过哪些服务
            risk_flags=list(dict.fromkeys(state.degraded_services)),
            # 只有“知识库问诊”这一项才影响回答的知识增强能力，故单独判定
            knowledge_degraded="knowledge_consult" in state.degraded_services,
        )

    # ============================================================
    # 工具方法区
    # 这些方法不再驱动主流程，只为主流程提供“无状态计算 / 轻量副作用”能力，
    # 大致分四组：追问合并与字段推断 → 风险标记收集 → 持久化与幂等 → 渲染与兜底。
    # 阅读顺序建议：_risk_flags → _merge_questions → _save_turn → _render。
    # ============================================================

    @staticmethod
    def _merge_questions(state: ConsultState, generated: list[str]) -> list[str]:
        """优先确定性缺失项；眼部场景不接受模型自行追加追问。"""
        # ① 先放“确定性缺失项”：由完整度检查器按缺失字段与风险优先级给出，
        #    它们的优先级始终高于模型自由发挥的追问
        merged: list[str] = []
        if state.completeness:
            merged.extend(state.completeness.questions)
        # ② 再决定是否接收模型追加的追问，以下三种情况一律不接收：
        #    - 眼部场景（domain == "eye"）：必须走固定问诊模板，防漏问关键项
        #    - keyword_thin：用户只说了一短句，规则已挑好最该问的
        #    - general_care：通用护理话题，继续追问没有边际收益
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
        # ③ 过滤“本轮之前已经问过的问题”，避免多轮对话里反复追同一件事
        asked = set(state.case_facts.asked_questions)
        # ④ 条数上限：信息极少的场景只给 2 条（降低用户回答负担），其余最多 3 条
        limit = (
            2
            if state.completeness
            and state.completeness.reason == "keyword_thin"
            else 3
        )
        # dict.fromkeys 去重且保持优先级顺序，最后按上限截断
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
        # 第一优先级：用户/档案已明确的物种，本方法只做规范化，不再推断
        current_species = (
            normalize_species(state.pet_info.species) if state.pet_info else None
        )
        if state.pet_info and current_species:
            # 别名归一（cat/猫/猫咪 → cat，dog/狗/犬 → dog）：
            # 否则下游按字符串比对物种时会漏匹配
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

        # 内嵌工具：从一段文字里“唯一地”推断物种。只做最朴素的关键词匹配，
        # 因为物种推断错会直接污染 RAG 过滤条件与风险规则选择。
        def infer_text(text: str) -> str | None:
            has_cat = "猫" in text
            has_dog = "狗" in text or "犬" in text
            # 猫狗同时出现（如“家里猫狗都有”）或都不出现 → 放弃推断，绝不猜
            if has_cat == has_dog:
                return None
            # 只有单侧命中才敢下结论
            return "cat" if has_cat else "dog"

        # 第二优先级：本轮用户文字（最新、最能代表当下意图）
        species = infer_text(state.text or "")
        source = "current_text"

        if species is None:
            # 第三/第四优先级：从最近一轮往前回溯历史，命中即停（越近越可信）
            for turn in reversed(state.history):
                history_species = normalize_species(
                    str((turn.pet_info or {}).get("species") or "")
                )
                # 3a) 历史档案里的 species：结构化字段，可信度最高
                if history_species:
                    species = history_species
                    source = "history_pet_info"
                    break
                # 3b) 历史用户原话（如“我家猫最近…”）
                history_species = infer_text(turn.user_text or "")
                if history_species:
                    species = history_species
                    source = "history_text"
                    break

        if species is None:
            # 全部推断失败：不写 pet_info，让下游按“未知物种”的通用策略处理
            state._species_source = "unknown"
            return
        # 落到档案：原先没档案就新建；已有档案只更新 species（保留名字/年龄等）
        if state.pet_info is None:
            state.pet_info = PetInfo(species=species)
        else:
            state.pet_info = state.pet_info.model_copy(update={"species": species})
        # 记录来源，供日志与问题定位（见 _species_source 取值说明）
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
        # 只取历史里的用户原话（assistant_answer 一律不要，否则助手那句
        # “建议尽快就医”会被急症规则当成用户描述的症状而误报）
        texts = [turn.user_text.strip() for turn in state.history if turn.user_text.strip()]
        # 本轮文字放最后：规则多为“就近匹配”，最新症状落在尾部更符合直觉；
        # 空字符串不入列，避免拼出多余的“。”分隔符
        if state.text.strip():
            texts.append(state.text.strip())
        # 用句号连接，让规则正则里的分句边界（如“但是”“不过”）依然有效
        return "。".join(texts)

    def _precheck_urgent(self, state: ConsultState) -> bool:
        """判断是否命中文字急症预判。

        :param state: 状态总线
        :return: True 如果 force_urgent_guidance 为 True
        """
        # 只认 force_urgent_guidance 这一个字段：单纯的 reasons 命中（如“呕吐”
        # 这类弱信号）不构成“必须走急症模板”的理由
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
        # 汇总顺序 = 由粗到精：文字预判 → 急症评估 → 综合分级。
        # 这样去重后保留的是“最早被发现”的那条原因，便于回溯触发源。
        flags: list[str] = []
        # ① 阶段 1 的纯规则急症预判（即使模型全挂也一定有值）
        if state.text_emergency_precheck:
            flags.extend(state.text_emergency_precheck.reasons)
        # ② 阶段 8 的急症语义评估（已合并图片红旗与宠物档案）
        if state.emergency_result:
            flags.extend(state.emergency_result.reasons)
        # ③ 阶段 8 的综合风险分级原因（LOW/MEDIUM/HIGH/EMERGENCY）
        if state.risk_result:
            flags.extend(state.risk_result.reasons)
        # ④ 降级标记与健康风险不同源，但同样需要让调用方看见
        flags.extend(state.degraded_services)  # 降级标记（如 vision/redis/knowledge_consult）
        # 去重保序：同一原因可能被多级评估同时命中
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
        # 局部导入：避免 schemas 层与本模块在 import 期形成循环依赖
        from app.schemas.conversation import ConversationTurn

        # 构建对话轮次记录
        # 说明：这里落的是“用户可见结果”的快照，而非 state 全量转储，
        # 字段选择以“下一轮能否复用 + 出问题时能否复盘”为准则。
        turn = ConversationTurn(
            turn_id="",              # 由存储层生成（列表自增/时间戳），此处仅占位
            created_at=utc_now_iso(),
            # —— 用户输入侧 ——
            user_text=state.text,
            # 档案整体保存：下一轮推断物种、与年龄相关的风险规则都要用
            pet_info=state.pet_info.model_dump() if state.pet_info else {},
            image_findings=state.vision_findings,
            # —— 风险评估侧 ——（风险等级兜底 LOW，None 会破坏历史统计）
            risk_level=response.risk_level or RiskLevel.LOW,
            risk_flags=response.risk_flags,
            # 存 status 字符串而非枚举：历史数据要能跨版本反序列化
            assistant_status=response.status.value,
            answer_mode=response.answer_mode,
            # 就医紧急度：无 vet_recommendation 时视为 NONE
            vet_urgency=(
                response.vet_recommendation.urgency
                if response.vet_recommendation else VetUrgency.NONE
            ),
            # —— 助手输出侧 ——
            assistant_answer=response.answer or "",
            follow_up_questions=response.follow_up_questions,
            # case_facts 只存有效字段：exclude_none 可避免历史里堆满 null
            case_facts=state.case_facts.model_dump(exclude_none=True),
            # V1.1 P1-6：记录模型/组件版本（§29），升级后可定位变化来源
            model_versions={
                "vision_model": self.s.consult_vision_model_name,
                "knowledge_provider": self.s.knowledge_provider,
                "knowledge_model": self.s.knowledge_model,
                "guard_model": self.s.consult_guard_model_name,
                "guard_mode": self.s.guard_mode,
                # prompt 版本写死字符串：prompt 是回答质量变化的高频来源，
                # 改 prompt 时必须同步改这里，否则无法区分“模型变了”还是“prompt 变了”
                "consult_prompt": "consult_answer_first_v2.4.0",
            },
        )
        # 写入交由 conversation_service：Agent 不关心底层是 Redis 还是其他存储
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
        # 调用方已保证“带幂等键”才会走到这里；assert 用于封死误用
        key = command.idempotency_key
        assert key is not None
        # 幂等域 = 租户 + 用户：不同租户/用户的同名 key 互不干扰
        tenant, user = command.auth.tenant_id, command.auth.user_id

        # 第一步：查缓存（命中即返；“是否同一请求”由 request_hash 参与判定）
        cached = await self.idempotency_repo.get_result(tenant, user, key, request_hash)
        if cached:
            # 直接反序列化历史响应：保证重试拿到与首次完全一致的结果
            return ConsultResponse.model_validate_json(cached)

        # 第二步：尝试占位（SETNX 语义）。返回 True = 抢到了执行权，
        # 此时返回 None，让主流程继续执行真正的问诊
        if await self.idempotency_repo.try_claim(
            tenant, user, key, request_hash, owner=owner
        ):
            return None

        # 第三步：轮询等待（已有请求在处理）
        # 记录等待起点，用于超时告警统计；__import__ 为既有写法，不改动其行为
        wait_started = __import__("time").monotonic()
        logger.info(
            "idempotency_wait_started",
            extra={"request_id": command.request_id},
        )
        # 循环条件用 has_remaining(0.2)：预留出一次 sleep 的时间，
        # 避免“还剩 0.1s 却仍 sleep 0.2s”造成的预算越界
        while deadline.has_remaining(0.2):
            # 3a) 持有者可能刚刚写入结果
            cached = await self.idempotency_repo.get_result(
                tenant, user, key, request_hash
            )
            if cached:
                return ConsultResponse.model_validate_json(cached)
            # 3b) 持有者失败释放占位 → 本请求接管并继续执行
            if await self.idempotency_repo.try_claim(
                tenant, user, key, request_hash, owner=owner
            ):
                return None
            await asyncio.sleep(0.2)

        # 第四步：超时处理
        # 抛异常而非返回错误响应：调用方需区分“并发冲突”与“业务失败”，
        # 且此路径下尚未产出任何可缓存的结果
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
        # 判定规则：只缓存“确定性终态”。
        # 反例：retryable=True 的 ERROR 若被缓存，用户重试时会直接命中上一次
        # 的失败结果，形成永久失败（因此必须先排除这一类）。
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
        # 参与指纹的只有“能改变回答结果”的四类输入。
        # 特别注意：不包含 idempotency_key 自身，也不包含 request_id / 时间戳，
        # 这样“同一问题重复提交”才能算出相同指纹并命中缓存。
        payload = {
            "conversation_id": command.conversation_id,
            "text": command.text,
            # mode="json" 保证枚举/日期等类型被规范化，否则同一档案可能算出不同指纹
            "pet_info": command.pet_info.model_dump(mode="json") if command.pet_info else None,
            # 用图片内容的 SHA-256 而非文件名/URL：重新上传同一张图仍视为同一请求
            "images": [image.sha256 for image in command.images],
        }
        # 规范化序列化三要素缺一不可：
        #   ensure_ascii=False → 中文不被转义（避免两侧编码口径不一致）
        #   sort_keys=True     → 字典序稳定（消除插入顺序影响）
        #   separators 去空格 → 消除序列化格式差异
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

        # 幂等标点工具：先剥掉句尾已有的标点（避免出现“。。”），再统一补句号
        def sentence(value: str) -> str:
            value = (value or "").strip().rstrip("。；;，,")
            return f"{value}。" if value else ""

        # 列表拼接工具：同样先清理标点，用中文分号连接，并按 limit 截断
        def joined(items: list[str], *, limit: int = 3) -> str:
            values = [str(item).strip().rstrip("。；;，,") for item in items if str(item).strip()]
            return "；".join(values[:limit])

        # 逐段拼装：每段只在“非空”时入列，最后用空行 join
        # （空行分段在客户端渲染时比单换行更易读）
        summary = sentence(g.summary)
        parts = [summary] if summary else []

        # 图片或明确事实只补充 summary 没有覆盖的内容，避免“已确认的情况”机械复述。
        # 去空白后做包含判断：容忍“多喝 水”这类空格差异导致的重复
        findings = [
            item for item in g.visible_findings
            if item and re.sub(r"\s+", "", item) not in re.sub(r"\s+", "", g.summary or "")
        ]
        if findings:
            # 图片类事实最多列 2 条：多了会喧宾夺主，盖过真正的主诉
            parts.append("从目前提供的信息看，" + sentence(joined(findings, limit=2)))
        # 病因属于“候选方向”，允许略多（最多 4 条）
        if g.possible_explanations:
            parts.append(
                "常见可以从这几个方向考虑："
                + sentence(joined(g.possible_explanations, limit=4))
            )
        # 行动指令类统一用“您现在可以先这样做：”，最多 3 条
        if g.what_to_do_now:
            parts.append("您现在可以先这样做：" + sentence(joined(g.what_to_do_now)))
        if g.what_to_monitor:
            parts.append("接下来重点留意：" + sentence(joined(g.what_to_monitor)))
        # “不要做”最多 2 条：禁忌项说太多用户反而记不住
        if g.avoid_actions:
            parts.append("暂时不要：" + sentence(joined(g.avoid_actions, limit=2)))
        vr = g.vet_recommendation
        # 只有 recommended=True 才渲染就医建议：否则“不推荐就医”也会被写成一句话
        if vr and vr.recommended:
            # 紧急程度标签词典：把枚举翻译成用户能立刻理解的行动口径
            urgency_label = {
                VetUrgency.EMERGENCY: "立即急诊", VetUrgency.URGENT: "尽快就医",
                VetUrgency.WITHIN_24_HOURS: "24 小时内就医", VetUrgency.BOOK_VET: "建议预约就医",
                VetUrgency.MONITOR: "密切观察", VetUrgency.NONE: "",
            }.get(vr.urgency, "")
            # prefix 为空时（urgency=NONE）不加“：”，直接输出原因句
            prefix = f"{urgency_label}：" if urgency_label else ""
            parts.append(prefix + sentence(vr.reason))
        # 免责声明固定收尾
        if g.disclaimer:
            parts.append(sentence(g.disclaimer))
        # 过滤空段后再拼接，保证不出现连续空行
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
        # 刻意写得“绝对安全”：不含任何诊断、病因或用药建议，
        # 只保留一条无条件成立的行动指引，因此可被任何失败分支复用
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
        # 顺序关闭即可：这些服务之间没有依赖关系，互不影响。
        # 注：rag_retriever 与各类规则引擎是纯本地对象，无需关闭。
        for svc in (
            self.image_service,
            self.moderation,
            self.consultation_service,
            self.conversation_service,
        ):
            # getattr 探测式调用：允许某个具体实现没有 close（如已被 mock 替换）
            close = getattr(svc, "close", None)
            if close is not None:
                await close()