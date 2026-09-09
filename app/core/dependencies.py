"""
依赖注入容器（v6.3 §19）

【文件定位】
这是问诊系统的"依赖容器"，负责在应用启动时一次性创建所有服务组件，
并在应用关闭时优雅释放资源。所有组件在生命周期内是单例，不每请求重建。

【设计原则】
1. 懒初始化：对象在 startup() 中统一创建，不在 __init__ 中初始化
2. 统一关闭：shutdown() 遍历所有组件，按依赖逆序关闭
3. 可选组件：队列化组件（Phase 2）仅在配置启用时初始化
4. 全局单例：FastAPI lifespan 的 startup/shutdown 管理，绝不每请求重建

【Container 的组件分类】
核心服务客户端：Redis、Vision、Guard、RAG
业务服务层：会话、图片、知识问诊、审核、医疗安全
Phase 2 队列化组件：TaskService、Worker、Outbox、监控（条件启用）
ConsultAgent 主控制器：编排所有服务的核心 Agent
限流器：基于 Redis 的请求频率控制

【启动流程】
FastAPI lifespan startup → Container.startup() → 创建所有组件 → 注入 Agent → 注册路由依赖
【关闭流程】
FastAPI lifespan shutdown → Container.shutdown() → 取消后台协程 → 关闭连接
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import Request

from app.agent.completeness_checker import CompletenessChecker
from app.agent.consult_agent import ConsultAgent
from app.agent.risk_engine import RiskEngine
from app.api.rate_limit import RateLimiter
from app.clients.guard_client import GuardClient
from app.clients.redis_client import RedisClient
from app.clients.vision_gateway_client import VisionGatewayClient
from app.core.config import Settings
from app.core.deadline import DeadlineFactory
from app.rag.loader import RagAssetLoader
from app.rag.emergency_shadow import V14EmergencyShadowMatcher
from app.rag.retriever import ShadowRetriever
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.idempotency_repository import IdempotencyRepository
from app.safety.emergency_rules import EmergencyRuleEngine
from app.safety.input_moderator import InputModerator
from app.safety.medical_checker import MedicalSafetyChecker
from app.safety.output_moderator import OutputModerator
from app.services.conversation_service import ConversationService
from app.services.consultation_service import ConsultationService
from app.services.dialogue_archive import DialogueArchive
from app.tasks.db import build_engine, build_session_factory
from app.tasks.models import Base as TaskBase
from app.tasks.migrations import migrate_v73_classified_admission
from app.tasks.dialogue import DialogueRepository
from app.tasks.gate import FastLaneGate
from app.tasks.metrics import QueueMonitor, TaskMetrics
from app.tasks.progress import TaskProgressStream
from app.tasks.mq import RocketMQConsultPublisher
from app.tasks.publisher import OutboxScanner
from app.tasks.service import TaskService
from app.tasks.worker import ConsultWorker, WorkerLoop
from app.services.image_service import ImageService
from app.services.knowledge_consult_service import KnowledgeConsultService
from app.services.medical_safety_service import MedicalSafetyService
from app.services.moderation_service import ModerationService

logger = logging.getLogger(__name__)


class _InlinePublisher:
    """单机/克隆验证直通发布器：消费端走 PG 轮询，不真发 RocketMQ。

    【用途】
    在开发/测试环境下，队列化组件启用但不需要真正的 RocketMQ。
    这个类实现 ConsultMqPublisherProto 接口，但 publish() 是空操作。
    OutboxScanner 随后会将任务标记为 published + mark_queued。

    【替换时机】
    新服务器配好 rocketmq-client-python 后，替换为 RocketMQConsultPublisher。
    两个实现均满足 ConsultMqPublisherProto 接口协议，可无缝切换。
    """

    async def publish(self, message) -> None:
        return  # OutboxScanner 随后标记 published + mark_queued

    async def close(self) -> None:
        return


class Container:
    """应用生命周期内的依赖对象集合。startup() 创建，shutdown() 关闭。

    【职责】
    1. 组装所有服务组件（Redis、视觉、审核、RAG、队列等）
    2. 管理组件生命周期（启动/关闭）
    3. 为 FastAPI 路由提供依赖注入入口（通过 get_container() 获取）

    【设计原则】
    - 懒初始化：对象在 startup() 中统一创建，不在 __init__ 中初始化
    - 统一关闭：shutdown() 遍历所有组件，按依赖逆序关闭
    - 可选组件：队列化组件（Phase 2）仅在配置启用时初始化

    【组件访问】
    路由层通过 request.app.state.container 获取 Container 实例，
    然后访问 container.agent、container.task_service 等组件。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._started = False

        # 核心服务客户端（应用启动时初始化）
        self.redis: RedisClient | None = None
        self.vision_client: VisionGatewayClient | None = None
        self.guard_client: GuardClient | None = None
        self.rag_retriever: ShadowRetriever | None = None
        self.rag_emergency_matcher: V14EmergencyShadowMatcher | None = None

        self.rate_limiter: RateLimiter | None = None
        self.agent: ConsultAgent | None = None

        # Phase 2 队列化组件（consult_mq_enabled 时有值）
        self.task_service: TaskService | None = None
        self.task_worker: ConsultWorker | None = None
        self.task_worker_loop: WorkerLoop | None = None
        self.dialogue_repo: DialogueRepository | None = None
        self.fast_gate: FastLaneGate | None = None
        self.outbox_scanner: OutboxScanner | None = None
        self.task_progress_stream: TaskProgressStream | None = None

    async def startup(self) -> None:
        """初始化所有依赖对象。

        【初始化顺序】
        1. Redis 客户端（支持 fakeredis mock 模式）
        2. 外部服务客户端（Vision、Guard）
        3. RAG 检索器（Shadow 检索 + 混合检索 + 急症匹配）
        4. 业务服务层（会话、图片、知识问诊、审核等）
        5. Phase 2 队列化组件（Worker、Outbox、监控）
        6. ConsultAgent 主控制器
        7. 限流器

        【注意】
        队列化组件的初始化是条件性的，仅在 consult_mq_enabled 时执行。
        所有组件初始化完成后，_started 标记为 True。
        """
        s = self.settings

        # ========== 1. Redis 客户端 ==========
        redis_backend = None
        if s.mock_mode:
            # Mock 模式：使用 fakeredis（无需真实 Redis 服务器）
            try:
                import fakeredis.aioredis
            except ImportError as exc:  # pragma: no cover - 本地安装错误
                raise RuntimeError(
                    'MOCK_MODE=true 需要安装开发依赖：pip install -e ".[dev]"'
                ) from exc
            redis_backend = fakeredis.aioredis.FakeRedis(decode_responses=True)

        self.redis = RedisClient(s, client=redis_backend)

        # 任务进度流（Redis Stream，用于 SSE 进度推送）
        self.task_progress_stream = TaskProgressStream(
            self.redis.client,
            s.redis_namespace,
            ttl_seconds=s.consult_sse_stream_ttl_seconds,
        )

        # ========== 2. 外部服务客户端 ==========
        self.vision_client = VisionGatewayClient(s)  # 图片分析（转发到 4B 视觉模型）
        self.guard_client = GuardClient(s)           # 内容审核（Guard 模型）

        # ========== 3. RAG 检索器 ==========
        if s.rag_shadow:
            # 加载 RAG 资产（卡片索引、急症规则等）
            asset_path = s.rag_index_path
            if not asset_path:
                asset_path = str(
                    Path(__file__).resolve().parents[2] / "assets" / "rag" / "v1_8"
                )
            report = RagAssetLoader(asset_path).load()

            if s.rag_embedding_model_path:
                # 混合检索模式（词面 + 向量）
                from app.rag.hybrid_retriever import HybridRetriever

                self.rag_retriever = HybridRetriever(
                    report,
                    top_k=s.rag_top_k,
                    threshold=s.rag_hybrid_threshold,
                    alpha=s.rag_hybrid_alpha,
                    model_path=s.rag_embedding_model_path,
                    fast_threshold=s.rag_fast_answer_threshold,
                )
                # 预热：模型加载 + 卡片向量缓存（启动期完成，首个请求不等待）
                try:
                    import time as _time

                    _t0 = _time.perf_counter()
                    if self.rag_retriever.warmup():
                        logger.info(
                            "混合检索预热完成（%.1fs, alpha=%.2f, threshold=%.2f）",
                            _time.perf_counter() - _t0, s.rag_hybrid_alpha, s.rag_hybrid_threshold,
                        )
                    else:
                        # warmup 非阻塞：模型后台加载中，就绪后自动启用混合模式
                        logger.info("混合检索后台预热中(模型就绪后自动启用)")
                except Exception:
                    logger.warning("混合检索预热异常, 回退纯词面模式", exc_info=True)
            else:
                # 纯词面检索模式（无向量）
                self.rag_retriever = ShadowRetriever(
                    report,
                    top_k=s.rag_top_k,
                    threshold=s.rag_score_threshold,
                    fast_threshold=s.rag_fast_answer_threshold,
                )

            if report.ready:
                logger.info(
                    "RAG Shadow 资产已加载（cards=%d version=%s format=%s）",
                    len(report.cards),
                    report.index_version,
                    report.asset_format,
                )
            else:
                logger.warning("RAG Shadow 资产不可用（errors=%s）", report.errors)

            # RAG 急症 Shadow 匹配器（独立急症规则检索）
            if s.rag_emergency_shadow:
                self.rag_emergency_matcher = V14EmergencyShadowMatcher.load(
                    asset_path, report.asset_format
                )
                emergency_report = self.rag_emergency_matcher.report
                if emergency_report.ready:
                    logger.info(
                        "RAG 急症 Shadow 资产已加载（rules=%d version=%s）",
                        len(emergency_report.rules),
                        emergency_report.version,
                    )
                else:
                    logger.warning(
                        "RAG 急症 Shadow 资产不可用（errors=%s）", emergency_report.errors
                    )

        # ========== 4. 业务服务层 ==========
        # 数据仓库层
        conversation_repo = ConversationRepository(s, client=self.redis.client)
        idempotency_repo = IdempotencyRepository(s, client=self.redis.client)

        # 规则引擎（急症规则 + 医疗安全检查）
        emergency_rules = EmergencyRuleEngine()
        await emergency_rules.load()
        medical_checker = MedicalSafetyChecker(s)
        await medical_checker.load()

        # 服务层
        self.conversation_service = ConversationService(s, conversation_repo)
        self.image_service = ImageService(
            s,
            self.vision_client,
            cache_client=self.redis.client,
        )
        knowledge_consult_service = KnowledgeConsultService(s)
        self.consultation_service = ConsultationService(s, knowledge_consult_service)
        self.moderation_service = ModerationService(s, self.guard_client)
        self.medical_safety_service = MedicalSafetyService(s, medical_checker)
        self.emergency_rules = emergency_rules

        # ========== 5. Phase 2 队列化组件（条件启用） ==========
        if s.consult_mq_enabled and s.consult_database_url:
            # 数据库引擎（PostgreSQL）
            task_engine = build_engine(
                s.consult_database_url,
                password=s.consult_database_password,
                pool_size=s.consult_db_pool_size,
                max_overflow=s.consult_db_max_overflow,
                pool_timeout=s.consult_db_pool_timeout_seconds,
            )
            # 创建表 + 迁移
            async with task_engine.begin() as conn:
                await conn.run_sync(TaskBase.metadata.create_all)
                await migrate_v73_classified_admission(conn)
            task_factory = build_session_factory(task_engine)

            self.task_service = TaskService(task_factory)

            # 直答并发闸门（Redis 原子计数）
            self.fast_gate = FastLaneGate(
                self.redis, s.redis_namespace, max_concurrent=s.fast_sync_max_concurrent
            )

            # ========== Worker 任务处理器 ==========
            async def normal_handler(task):
                """普通问诊任务处理器（需要大模型生成）。

                工作流程：
                1. 从 task.payload 重建 ConsultCommand
                2. 创建 progress 回调（发布到 Redis Stream）
                3. 调用 agent.run(command, progress=progress)
                4. 保存结果到 PostgreSQL
                5. 清理临时图片文件
                """
                from app.tasks.runner import build_command_from_task, cleanup_task_temp_files

                try:
                    # 重建 ConsultCommand（从 task.payload）
                    command = build_command_from_task(task)

                    # 进度回调（发布到 Redis Stream，供 SSE 读取）
                    progress = None
                    if (task.payload or {}).get("stream_events") and self.task_progress_stream is not None:
                        await self.task_progress_stream.publish(
                            task.request_id,
                            "processing",
                            {"stage": "processing", "message": "任务开始处理"},
                        )

                        async def progress(event: str, data: dict) -> None:
                            await self.task_progress_stream.publish(
                                task.request_id,
                                event,
                                {
                                    "stage": event,
                                    "message": {
                                        "input_reviewed": "输入审核完成",
                                        "vision_started": "正在分析图片",
                                        "vision_completed": "图片分析完成",
                                        "risk_assessed": "风险评估完成",
                                        "answer_generated": "建议生成完成，正在进行安全审核",
                                        "medical_review_completed": "医疗安全审核完成",
                                        "output_review_completed": "输出审核完成",
                                    }.get(event, ""),
                                    **data,
                                },
                            )

                    # 执行问诊
                    response = await self.agent.run(command, progress=progress)
                    await self.task_service.save_result(
                        task.id, response.model_dump(mode="json")
                    )
                finally:
                    # 清理临时图片文件（无论成功失败）
                    cleanup_task_temp_files(task)

            async def fast_handler(task):
                """直答任务处理器（RAG 检索命中，无需大模型）。

                工作流程：
                1. 检查 payload 是否有预渲染结果（同步车道场景）
                2. 有 → 直接保存结果
                3. 无 → 重新检索 + 卡片直答（溢出场景）
                """
                # 同步车道：登记时已渲染存入 payload
                fast_result = task.payload.get("fast_result")
                if fast_result:
                    await self.task_service.save_result(task.id, fast_result)
                    return

                # 溢出场景：Worker 内重新渲染直答
                from app.schemas.consult import GeneratedConsultation, VetRecommendation
                from app.core.constants import AnswerMode, RiskLevel, VetUrgency, DEFAULT_DISCLAIMER

                rag_result = self.rag_retriever.search(
                    task.payload.get("text", ""),
                    species=(task.payload.get("pet_info") or {}).get("species"),
                )
                payload = self.rag_retriever.build_fast_answer(rag_result)
                generated = GeneratedConsultation(
                    summary=payload["summary"],
                    what_to_do_now=payload["what_to_do_now"],
                    follow_up_questions=payload["follow_up_questions"],
                    possible_explanations=payload["possible_explanations"],
                    avoid_actions=payload["avoid_actions"],
                    what_to_monitor=payload["what_to_monitor"],
                    risk_level=RiskLevel.LOW,
                    answer_mode=AnswerMode.NORMAL,
                    vet_recommendation=VetRecommendation(
                        recommended=False, urgency=VetUrgency.NONE
                    ),
                    disclaimer=DEFAULT_DISCLAIMER,
                )
                from app.agent.consult_agent import ConsultAgent

                answer = (generated.answer_text or ConsultAgent._render(generated))
                await self.task_service.save_result(
                    task.id,
                    {
                        "request_id": task.request_id,
                        "conversation_id": task.conversation_id,
                        "status": "success",
                        "answer_mode": "normal",
                        "answer": answer,
                        "risk_level": "low",
                        "risk_flags": [],
                        "image_findings": [],
                        "follow_up_questions": payload["follow_up_questions"],
                        "disclaimer": DEFAULT_DISCLAIMER,
                    },
                )

            # 任务指标收集
            task_metrics = TaskMetrics()
            self.dialogue_repo = DialogueRepository(task_factory)

            # 对话存档清理（30 天清理一次，每天执行）
            async def dialogue_cleanup_loop():
                while True:
                    try:
                        removed = await self.dialogue_repo.cleanup_older_than(days=30)
                        if removed:
                            logger.info("dialogue_cleanup_removed", extra={"rows": removed})
                    except Exception:  # noqa: BLE001
                        logger.exception("dialogue_cleanup_error")
                    await asyncio.sleep(86400)

            self._dialogue_cleanup_task = asyncio.create_task(dialogue_cleanup_loop())

            # 多 Worker 并发（v1.5）：共享 GPU，FOR UPDATE SKIP LOCKED 抢占互不重复
            self.task_workers: list[ConsultWorker] = []
            self._worker_tasks: list[asyncio.Task] = []
            for _i in range(1, s.consult_worker_count + 1):
                worker = ConsultWorker(
                    task_factory,
                    self.task_service,
                    fast_handler=fast_handler,
                    normal_handler=normal_handler,
                    worker_id=f"consult-worker-{_i}",
                    max_retry=s.consult_mq_max_retry,
                    metrics=task_metrics,
                )
                self.task_workers.append(worker)
                self._worker_tasks.append(
                    asyncio.create_task(WorkerLoop(worker).run())
                )
            self.task_worker = self.task_workers[0]
            self.task_worker_loop = None  # 兼容引用（多循环已展开）
            logger.info("问诊 Worker 已启动: %d 个", s.consult_worker_count)

            # Outbox 发布循环：registered → published → queued
            # 队列化开启时真发独立 RocketMQ（pyrocketmq，集群 pet-consult :9877）
            # 关闭时退回直通发布器（_InlinePublisher，仅 PG 流转）
            if s.consult_mq_enabled:
                publisher = RocketMQConsultPublisher(
                    namesrv_addr=s.consult_mq_namesrv_addr,
                    topic=s.consult_mq_topic,
                    classpath=s.consult_mq_classpath,
                )
            else:
                publisher = _InlinePublisher()

            scanner = OutboxScanner(task_factory, publisher, self.task_service)
            self.outbox_scanner = scanner

            async def outbox_loop():
                while True:
                    try:
                        await scanner.scan_once()
                    except Exception:  # noqa: BLE001 - 循环不得中断
                        logger.exception("outbox_scan_error")
                    await asyncio.sleep(s.consult_outbox_scan_seconds)

            self._outbox_task = asyncio.create_task(outbox_loop())

            # 队列监控：定期输出 queue_depth / oldest_wait / p95（最小监控 2.7）
            queue_monitor = QueueMonitor(task_factory, task_metrics, self.task_service)

            async def monitor_loop():
                while True:
                    try:
                        # 清理过期活跃任务
                        expired = await self.task_service.expire_stale_active(
                            s.queue_active_stale_seconds
                        )
                        if expired:
                            logger.warning(
                                "queue_stale_tasks_expired",
                                extra={"count": expired},
                            )
                        await queue_monitor.log_once()
                    except Exception:  # noqa: BLE001
                        logger.exception("queue_monitor_error")
                    await asyncio.sleep(30)

            self._monitor_task = asyncio.create_task(monitor_loop())
            logger.info(
                "队列化已启用（worker=%s db=%s classified=%s text_max=%s "
                "image_max=%s image_slots=%s legacy_global=%s）",
                "consult-worker-1",
                s.consult_database_url.split("@")[-1],
                s.classified_admission_enabled,
                s.text_max_active,
                s.image_max_active,
                s.image_max_active_slots,
                s.queue_max_pending,
            )
        else:
            # 队列未启用 → 清空相关组件
            self._worker_task = None
            self._outbox_task = None
            self._monitor_task = None
            self.dialogue_repo = None
            self.fast_gate = None
            self.outbox_scanner = None

        # ========== 6. 对话存档 ==========
        dialogue_archive = DialogueArchive(s.dialogue_archive_path or None)
        if dialogue_archive.enabled:
            logger.info("对话存档已启用（path=%s）", dialogue_archive.path)

        # ========== 7. ConsultAgent 主控制器 ==========
        self.agent = ConsultAgent(
            settings=s,
            image_service=self.image_service,
            moderation_service=self.moderation_service,
            input_moderator=InputModerator(),
            output_moderator=OutputModerator(),
            emergency_rules=emergency_rules,
            conversation_service=self.conversation_service,
            completeness_checker=CompletenessChecker(),
            risk_engine=RiskEngine(s, emergency_rules),
            consultation_service=self.consultation_service,
            medical_safety_service=self.medical_safety_service,
            idempotency_repo=idempotency_repo,
            deadline_factory=DeadlineFactory(s.consult_total_timeout_seconds),
            rag_retriever=self.rag_retriever,
            rag_emergency_matcher=self.rag_emergency_matcher,
            dialogue_archive=dialogue_archive,
            dialogue_repo=self.dialogue_repo if s.consult_mq_enabled else None,
        )

        # ========== 8. 限流器 ==========
        self.rate_limiter = RateLimiter(s, client=self.redis.client)

        self._started = True
        logger.info(
            "容器启动完成（mock=%s provider=%s）",
            s.mock_mode,
            s.knowledge_provider,
        )

    async def shutdown(self) -> None:
        """优雅关闭所有依赖对象。

        【关闭顺序】
        1. 取消后台协程任务（Worker 循环、Outbox 扫描、监控、对话清理）
        2. 关闭服务客户端连接（Agent、限流器、Redis、Vision、Guard）

        【幂等安全】
        多次调用不会引发异常，_started 标记确保只执行一次。
        """
        if not self._started:
            return
        self._started = False

        # 1. 取消后台协程
        if getattr(self, "_worker_task", None) is not None:
            if self.task_worker_loop is not None:
                self.task_worker_loop.stop()
            self._worker_task.cancel()
        if getattr(self, "_outbox_task", None) is not None:
            self._outbox_task.cancel()
        if getattr(self, "_monitor_task", None) is not None:
            self._monitor_task.cancel()
        if getattr(self, "_dialogue_cleanup_task", None) is not None:
            self._dialogue_cleanup_task.cancel()

        # 2. 关闭外部连接
        for obj in (
            self.agent, self.rate_limiter, self.redis,
            self.vision_client, self.guard_client,
        ):
            close = getattr(obj, "close", None)
            if close is not None:
                await close()

        logger.info("容器已关闭")


def get_container(request: Request) -> Container:
    """从 FastAPI 请求中获取依赖容器实例。

    【用途】
    FastAPI 依赖注入：Depends(get_container)
    返回 request.app.state.container（全局单例）
    """
    return request.app.state.container


def get_app_settings(request: Request) -> Settings:
    """从请求中获取容器 settings（不用全局缓存单例，测试可注入不同配置）。

    【设计要点】
    不直接使用全局 Settings 单例，而是从 Container 获取。
    这样测试时可以注入不同的配置对象，提高可测试性。
    """
    return request.app.state.container.settings


def get_agent(request: Request) -> ConsultAgent:
    """从请求中获取 ConsultAgent 实例。

    【用途】
    FastAPI 依赖注入：Depends(get_agent)
    返回 container.agent（全局单例，生命周期内只创建一次）
    """
    return request.app.state.container.agent


def get_rate_limiter(request: Request) -> RateLimiter:
    """从请求中获取限流器实例。

    【用途】
    FastAPI 依赖注入：Depends(get_rate_limiter)
    返回 container.rate_limiter（基于 Redis 的请求频率控制）
    """
    return request.app.state.container.rate_limiter