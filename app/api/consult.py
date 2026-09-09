"""POST /api/v1/consult（v5 §10.1：multipart/form-data + Idempotency-Key）

【文件定位】
这是问诊系统的 HTTP 入口层，负责将外部 HTTP 请求转换为内部 ConsultCommand 对象。
路由层只做"协议转换"：HTTP 表单 → ConsultCommand，不处理任何模型推理或业务逻辑。

【两条执行路径】
路径 A（直连同步）：CONSULT_MQ_ENABLED=false
  POST /api/v1/consult → _prepare_command() → agent.run(command) → ConsultResponse
  适用于：开发环境、纯文本问诊、快速验证

路径 B（队列化异步）：CONSULT_MQ_ENABLED=true（生产环境）
  POST /api/v1/consult → _prepare_command() → _register_queue_task() → PostgreSQL 落库
    → RocketMQ 发布 → Worker 消费 → agent.run(command) → 结果存 PG → 客户端轮询/SSE 获取
  适用于：生产环境、带图片的问诊、高并发场景

【SSE 流式输出】
POST /api/v1/consult/stream → 返回 Server-Sent Events 流
  客户端可实时看到处理进度：accepted → input_reviewed → vision_started → ... → final
  队列模式下通过 Redis Stream 中转进度事件，直连模式通过 asyncio.Queue 中转

【关键设计】
1. 输入验证集中在 _prepare_command()，两个端点复用
2. 图片数据通过临时文件传递给 Worker（不存数据库，避免大对象）
3. 急症预判（0ms 纯规则）在队列登记前执行，命中则同步返回固定模板
4. RAG 直答预判（~40ms 检索）有并发闸门控制，额度满则溢出到队列
5. SSE 连接有界等待（consult_sse_max_wait_seconds），防止客户端无限挂起
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.agent.consult_agent import ConsultAgent
from app.api.rate_limit import RateLimiter
from app.core.config import Settings
from app.core.dependencies import get_agent, get_app_settings, get_rate_limiter
from app.core.constants import ConsultStatus
from app.core.exceptions import ImageTooLargeError, RateLimitError, RequestValidationError
from app.core.security import get_auth_context
from app.image.processor import process_image
from app.schemas.auth import AuthContext
from app.schemas.common import ErrorDetail
from app.schemas.consult import ConsultCommand, ConsultResponse
from app.schemas.pet import PetInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["consult"])

# 各处理阶段的人类可读消息（用于 SSE 进度事件推送）
_STAGE_MESSAGES = {
    "input_reviewed": "输入审核完成",
    "vision_started": "正在分析图片",
    "vision_completed": "图片分析完成",
    "risk_assessed": "风险评估完成",
    "answer_generated": "建议生成完成，正在进行安全审核",
    "medical_review_completed": "医疗安全审核完成",
    "output_review_completed": "输出审核完成",
    "urgent_guidance": "检测到急症信号，先返回安全处置建议",
}


async def _register_queue_task(
    *,
    request: Request,
    command: ConsultCommand,
    pre_answered: bool = False,
    priority: str = "P1",
    fast_path: bool = False,
    fast_result: dict | None = None,
    enforce_capacity: bool = False,
    stream_events: bool = False,
) -> int | None:
    """队列化登记（consult_mq_enabled 时）：图片写临时文件 + task/outbox 落库。

    【职责】
    将问诊请求登记到 PostgreSQL 任务表，为后续 Worker 异步处理做准备。
    这是队列化执行路径的第一步，负责：
    1. 将图片数据写入临时文件（Worker 通过文件路径读取，不经过数据库）
    2. 构建任务 payload（包含所有问诊上下文）
    3. 调用 TaskService.register_task() 落库（带容量控制）
    4. 立即触发 Outbox 扫描（不等 5s 定时扫描，降低延迟从 ~5s 到 ~1s）

    【设计要点】
    - 图片临时文件路径：/tmp/pet-consult-images/{request_id}/{image_id}.{format}
    - 任务分类：image（有图片）/ text（纯文本），支持分类限流
    - 容量控制：enforce_capacity=True 时，队列满抛 QueueBusyError → HTTP 503
    - 异常安全：登记失败时自动清理临时图片目录

    【参数说明】
    :param request: FastAPI 请求对象（用于获取 container）
    :param command: 问诊命令对象（包含全部问诊上下文）
    :param pre_answered: 是否已预回答（急症场景，Worker 只需补记账）
    :param priority: 任务优先级（P0=急症, P1=普通, P2=低优）
    :param fast_path: 是否直答路径（RAG 检索命中，无需大模型生成）
    :param fast_result: 直答结果（已渲染好的响应，Worker 直接返回）
    :param enforce_capacity: 是否强制容量控制（满则拒绝）
    :param stream_events: 是否启用 SSE 进度事件（流式输出模式）
    :return: 任务 ID（登记成功）或 None（队列未启用）
    """
    container = request.app.state.container
    # 队列服务未启用 → 跳过登记（走直连同步路径）
    if not container.task_service:
        return None

    # 构建任务 payload（Worker 重建 ConsultCommand 的数据源）
    payload: dict = {
        "text": command.text,
        "pet_info": (
            command.pet_info.model_dump(mode="json") if command.pet_info else None
        ),
        "pets": [p.model_dump(mode="json") for p in command.pets],
        "pet_ref": command.pet_ref,
        "images": [],  # 图片元数据（临时文件路径）
        "stream_events": stream_events,
    }

    # 图片处理：写入临时文件 + 记录元数据
    temp_base = None
    if command.images:
        import tempfile
        from pathlib import Path as _Path

        # 临时目录：/tmp/pet-consult-images/{request_id}/
        base = _Path(tempfile.gettempdir()) / "pet-consult-images" / command.request_id
        base.mkdir(parents=True, exist_ok=True)
        temp_base = base
        for img in command.images:
            meta = {
                "image_id": img.image_id,
                "filename": img.filename,
                "format": img.format,
                "width": img.width,
                "height": img.height,
                "sha256": img.sha256,
                "temp_path": "",
            }
            if img.data:
                # 写入临时文件：/tmp/pet-consult-images/{request_id}/{image_id}.{format}
                path = base / f"{img.image_id}.{img.format.lower()}"
                path.write_bytes(img.data)
                meta["temp_path"] = str(path)
            payload["images"].append(meta)

    # 直答结果（RAG 检索命中时）
    if fast_result is not None:
        payload["fast_result"] = fast_result

    # 任务分类（用于分类限流：图片任务和文本任务分开管理）
    image_count = len(command.images)
    task_kind = "image" if image_count else "text"
    payload["task_kind"] = task_kind
    payload["image_count"] = image_count

    # 容量控制参数（分类准入：v1.5 新增）
    classified = container.settings.classified_admission_enabled
    try:
        task_id = await container.task_service.register_task(
            request_id=command.request_id,
            tenant_id=command.auth.tenant_id,
            user_id=command.auth.user_id,
            conversation_id=command.conversation_id,
            priority=priority,
            pre_answered=pre_answered,
            fast_path=fast_path,
            payload=payload,
            # 全局容量控制（旧版）
            max_active=(
                container.settings.queue_max_pending
                if enforce_capacity and not classified
                else 0
            ),
            # 分类容量控制（新版：文本和图片分开限流）
            task_kind=task_kind,
            image_count=image_count,
            text_max_active=(
                container.settings.text_max_active
                if enforce_capacity and classified
                else 0
            ),
            image_max_active=(
                container.settings.image_max_active
                if enforce_capacity and classified
                else 0
            ),
            image_max_active_slots=(
                container.settings.image_max_active_slots
                if enforce_capacity and classified
                else 0
            ),
            stale_after_seconds=container.settings.queue_active_stale_seconds,
        )
    except Exception:
        # 登记失败 → 清理临时图片目录（避免磁盘泄漏）
        if temp_base is not None:
            import shutil
            shutil.rmtree(temp_base, ignore_errors=True)
        raise

    # 立即触发 Outbox 扫描（不等 5s 定时扫描）：将 registered 任务发布到 RocketMQ
    # 效果：溢出任务延迟从 ~5s 降到 ~1s
    if container.outbox_scanner is not None:
        try:
            await container.outbox_scanner.scan_once(limit=10)
        except Exception:  # noqa: BLE001 - 发布失败由定时扫描补发，不阻断主流程
            logger.warning("immediate_outbox_scan_failed", exc_info=True)

    return task_id


async def _execute_via_queue(
    *,
    request: Request,
    command: ConsultCommand,
    agent: ConsultAgent,
) -> ConsultResponse:
    """队列化执行：急症/直答 API 同步（秒回）；症状问诊走队列异步。

    【执行策略（三条车道）】
    车道 1：急症预判（0ms 纯规则）
      → 同步返回固定急症模板 + 登记 pre_answered 任务（Worker 补记账）
      → 用户体验：秒回，无等待

    车道 2：RAG 直答预判（~40ms 检索）
      → 有并发闸门（Redis 原子计数），额度内同步执行（<100ms）
      → 额度满 → 溢出到队列（Worker 内渲染直答）
      → 用户体验：大部分秒回，高峰期排队

    车道 3：普通症状问诊（需要大模型生成）
      → 登记任务 → 等待 Worker 处理 → 轮询结果
      → 用户体验：排队等待（通常几秒到几十秒）

    【设计哲学】
    - 先落库后响应：任何情况都先登记任务，保证数据不丢失
    - 急症优先：急症不走队列，直接同步返回
    - 直答加速：RAG 能回答的尽量不走大模型
    - 容量保护：队列满返回 HTTP 503，防止系统过载
    """
    from app.core.constants import RiskLevel

    container = request.app.state.container
    settings = container.settings

    # ========== 车道 1：急症预判（纯规则，0ms） ==========
    # 急症信号命中 → 同步返回固定模板 + 登记记账任务
    # Worker 收到 pre_answered=True 的任务后，只做补记账，不调 Agent
    precheck = agent.emergency_rules.precheck_text(
        text=command.text, pet_info=command.pet_info
    )
    if precheck.level == RiskLevel.EMERGENCY:
        # 登记 pre_answered 任务（Worker 补记账）
        await _register_queue_task(
            request=request, command=command, pre_answered=True, priority="P0"
        )
        # 同步返回急症固定模板（不调 Agent，秒回）
        return await agent.run(command)

    # ========== 车道 2：RAG 直答预判（~40ms 检索） ==========
    if settings.rag_fast_answer and container.rag_retriever is not None:
        rag_result = container.rag_retriever.search(
            command.text,
            species=command.pet_info.species if command.pet_info else None,
        )
        if container.rag_retriever.is_fast_answerable(rag_result):
            # 并发闸门（Redis 原子计数）：限制同时直答的请求数
            acquired = False
            if container.fast_gate is not None:
                acquired = await container.fast_gate.acquire()

            if acquired:
                # 有额度 → 同步执行直答（<100ms）
                try:
                    response = await agent.run(command)
                    # 登记 fast_path 任务（记录已渲染的结果）
                    task_id = await _register_queue_task(
                        request=request, command=command, fast_path=True,
                        fast_result=response.model_dump(mode="json"),
                    )
                    if task_id is not None:
                        await container.task_service.save_result(
                            task_id, response.model_dump(mode="json")
                        )
                    return response
                finally:
                    # 释放闸门令牌（无论成功失败）
                    if container.fast_gate is not None:
                        await container.fast_gate.release()

            # 额度满 → 溢出到队列（Worker 内渲染直答）
            # 容量统计与登记在 PostgreSQL 同一事务内完成
            # 满载抛 QueueBusyError → HTTP 503
            task_id = await _register_queue_task(
                request=request, command=command, fast_path=True, enforce_capacity=True
            )
            if task_id is not None:
                # 等待 Worker 处理完成
                result = await container.task_service.wait_result(
                    command.request_id,
                    timeout_seconds=container.settings.consult_wait_result_timeout_seconds,
                )
                if result is not None:
                    return ConsultResponse.model_validate(result)

    # ========== 车道 3：普通症状问诊（需要大模型生成） ==========
    # 原子准入：容量满由统一异常处理器返回真实 HTTP 503
    task_id = await _register_queue_task(
        request=request, command=command, enforce_capacity=True
    )
    if task_id is None:
        # 队列未启用 → 降级为直连同步
        return await agent.run(command)

    # 等待 Worker 处理完成（轮询 PostgreSQL 结果表）
    result = await container.task_service.wait_result(
        command.request_id,
        timeout_seconds=container.settings.consult_wait_result_timeout_seconds,
    )
    if result is None:
        # 超时 → 返回错误响应（可重试）
        return ConsultResponse(
            request_id=command.request_id,
            conversation_id=command.conversation_id,
            status=ConsultStatus.ERROR,
            retryable=True,
            error=ErrorDetail(
                code="TASK_TIMEOUT",
                message="处理超时，请稍后重试",
                retryable=True,
            ),
        )

    return ConsultResponse.model_validate(result)


def _sse(event: str, data: dict[str, Any]) -> str:
    """构造 SSE（Server-Sent Events）事件字符串。

    【SSE 协议格式】
    event: {event_name}
    data: {json_payload}
    <空行>

    【用途】
    前端通过 EventSource 或 fetch + ReadableStream 接收实时进度事件。
    每个事件包含 event 类型和 data JSON 载荷。

    :param event: 事件名称（accepted / input_reviewed / final / error 等）
    :param data: 事件数据（会被序列化为 JSON）
    :return: 符合 SSE 协议的字符串
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def _prepare_command(
    *,
    request: Request,
    conversation_id: str,
    text: str,
    pet_info: str,
    pet_ref: str | None,
    images: list[UploadFile],
    idempotency_key: str | None,
    auth: AuthContext,
    rate: RateLimiter,
    settings: Settings,
) -> ConsultCommand:
    """验证并转换 HTTP 表单输入为 ConsultCommand 对象。

    【职责】
    这是 HTTP 层的"协议转换"函数，负责：
    1. 限流检查（RateLimiter）
    2. 输入验证（text/images 至少一项、图片数量 ≤3、图片大小 ≤5MB）
    3. pet_info 解析（兼容单对象和数组两种格式）
    4. 图片处理（读取二进制 → process_image → ProcessedImage 列表）
    5. 环境校验（模拟图片分析环境下禁止发送真实问诊）

    【多宠支持（2026-08-19）】
    pet_info 兼容两种格式：
    - 单对象：{"species": "dog", ...}（旧格式，自动包一层为 [pet]）
    - 数组：  [{"name": "豆豆", ...}, {"name": "咪咪", ...}]（新格式）
    pet_ref：可选，指定本次问诊的宠物（name 或下标字符串如 "0"）

    【参数说明】
    :param request: FastAPI 请求对象（用于获取 request_id）
    :param conversation_id: 会话 ID（用于关联同一会话的多轮问诊）
    :param text: 问诊文本（用户描述的症状）
    :param pet_info: 宠物信息 JSON 字符串（单对象或数组）
    :param pet_ref: 本次问诊指向的宠物标识（可选）
    :param images: 上传的图片文件列表（FastAPI UploadFile）
    :param idempotency_key: 防重键（HTTP Header: Idempotency-Key）
    :param auth: 认证上下文（tenant_id / user_id）
    :param rate: 限流器（检查请求频率）
    :param settings: 应用配置
    :return: ConsultCommand 对象（Agent 的输入契约）
    :raises RateLimitError: 请求过于频繁
    :raises RequestValidationError: 输入格式错误
    :raises ImageTooLargeError: 图片超过 5MB
    """
    # 限流检查（防止用户请求过快）
    if not await rate.allow(auth.tenant_id, auth.user_id):
        raise RateLimitError("请求过于频繁，请稍后再试")

    # 过滤有效图片（有文件名的）
    real_images = [f for f in images if f.filename]
    # 至少要有文本或图片
    if not text.strip() and not real_images:
        raise RequestValidationError("text 和 images 至少提供一项")

    # 解析宠物信息（兼容单对象和数组）
    pets: list[PetInfo] = []
    if pet_info.strip():
        try:
            raw = json.loads(pet_info)
            if isinstance(raw, list):
                # 数组格式：多宠支持
                pets = [PetInfo.model_validate(item) for item in raw]
            elif isinstance(raw, dict):
                # 单对象格式：旧版兼容，自动包一层
                pets = [PetInfo.model_validate(raw)]
            else:
                raise ValueError("pet_info 必须是对象或数组")
        except Exception as exc:  # noqa: BLE001
            raise RequestValidationError("pet_info 格式不正确") from exc

    # 图片数量限制
    if len(real_images) > 3:
        raise RequestValidationError("最多 3 张图片")

    # 环境校验：模拟图片分析环境下禁止发送真实问诊
    if (
        real_images
        and settings.mock_vision
        and not settings.mock_knowledge_consult
    ):
        raise RequestValidationError(
            "当前联调环境使用模拟图片分析，不能把模拟观察发送给真实问诊服务"
        )

    # 图片处理：读取二进制 → 验证大小 → process_image 标准化
    processed = []
    for f in real_images:
        raw = await f.read(settings.max_image_bytes + 1)
        if len(raw) > settings.max_image_bytes:
            raise ImageTooLargeError("单张图片不能超过 5MB")
        processed.append(
            process_image(
                raw,
                image_id=f"img_{len(processed) + 1}",
                filename=f.filename or "upload",
                settings=settings,
            )
        )

    # 构建 ConsultCommand（Agent 的输入契约）
    return ConsultCommand(
        request_id=request.state.request_id,
        auth=auth,
        conversation_id=conversation_id,
        text=text,
        images=processed,
        pets=pets,
        pet_ref=pet_ref,
        idempotency_key=idempotency_key,
    )


async def _queue_event_stream(
    *,
    request: Request,
    command: ConsultCommand,
    agent: ConsultAgent,
    task_id: int | None,
):
    """队列化 SSE：任务已原子登记 → Redis Stream 阶段事件 → 结果 final。

    【职责】
    在队列模式下，为客户端提供实时进度推送。工作流程：
    1. 从 Redis Stream 读取 Worker 发布的进度事件
    2. 轮询 PostgreSQL 检查任务最终状态
    3. 发送 SSE 事件给客户端（accepted → 进度事件 → final/error）

    【有界等待设计（2026-08-20）】
    - 最大等待时间：consult_sse_max_wait_seconds
    - 超时后发送 error(QUEUE_TIMEOUT, retryable) 优雅收尾
    - 避免突发积压时客户端无限挂起
    - 每 10 秒发送一次 keep-alive 心跳（防止连接超时断开）

    【容错设计】
    - Redis Stream 读取失败 → 降级为轮询 PostgreSQL
    - 客户端断开 → 取消任务（cancel_by_request_id）
    - 任务失败 → 发送 error 事件（包含失败原因）

    :param request: FastAPI 请求对象
    :param command: 问诊命令对象
    :param agent: 问诊 Agent（task_id=None 时降级为直连）
    :param task_id: 任务 ID（登记成功时有值）
    """
    import asyncio as _asyncio
    import time as _time

    container = request.app.state.container
    settings = container.settings

    # task_id=None → 队列未启用，降级为直连 SSE
    if task_id is None:
        async for chunk in _consult_event_stream(request=request, command=command, agent=agent):
            yield chunk
        return

    # 发送"已接受"事件
    yield _sse("accepted", {"request_id": command.request_id, "stage": "accepted", "message": "请求已接收，排队处理中"})

    # 计算 deadline（有界等待）
    deadline = _time.monotonic() + settings.consult_sse_max_wait_seconds
    last_id = "0-0"  # Redis Stream 起始游标
    last_heartbeat = _time.monotonic()

    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            # 超时 → 发送错误事件（可重试）
            yield _sse(
                "error",
                {
                    "request_id": command.request_id,
                    "error": {
                        "code": "QUEUE_TIMEOUT",
                        "message": "排队等待超时，请稍后重试",
                        "retryable": True,
                    },
                },
            )
            return

        # 从 Redis Stream 读取进度事件
        if container.task_progress_stream is not None:
            try:
                last_id, events = await container.task_progress_stream.read(
                    command.request_id,
                    last_id,
                    block_ms=min(1000, max(1, int(remaining * 1000))),
                )
                for event, data in events:
                    yield _sse(
                        event,
                        {"request_id": command.request_id, **data},
                    )
            except Exception:  # noqa: BLE001 - 事件通道失败仍可轮询最终结果
                logger.warning(
                    "queue_sse_progress_read_failed",
                    extra={"request_id": command.request_id},
                    exc_info=True,
                )
                await _asyncio.sleep(min(1.0, remaining))
        else:
            await _asyncio.sleep(min(1.0, remaining))

        # 轮询 PostgreSQL 检查任务最终状态
        task = await container.task_service.get_by_request_id(command.request_id)
        if task is not None and task.status == "completed" and task.result_json:
            # 任务完成 → 发送最终结果
            yield _sse(
                "final",
                {"request_id": command.request_id, "response": task.result_json},
            )
            return

        if task is not None and task.status in ("failed", "dead_letter", "cancelled", "timeout"):
            # 任务失败 → 发送错误事件
            yield _sse(
                "error",
                {
                    "request_id": command.request_id,
                    "error": {
                        "code": "TASK_" + task.status.upper(),
                        "message": "任务处理失败（" + task.status + "）",
                        "retryable": True,
                    },
                },
            )
            return

        # 客户端断开 → 取消任务
        if await request.is_disconnected():
            await container.task_service.cancel_by_request_id(command.request_id, "client_disconnected")
            return

        # 心跳机制：每 10 秒发送一次 keep-alive（防止连接超时）
        if _time.monotonic() - last_heartbeat >= 10.0:
            yield ": keep-alive\n\n"
            last_heartbeat = _time.monotonic()


async def _consult_event_stream(
    *,
    request: Request,
    command: ConsultCommand,
    agent: ConsultAgent,
) -> AsyncIterator[str]:
    """直连模式 SSE：Agent 处理过程中实时推送进度事件。

    【职责】
    在直连模式（非队列化）下，为客户端提供实时进度推送。工作流程：
    1. 创建 asyncio.Queue 作为进度事件缓冲区
    2. 启动 Agent.run_stream() 后台任务（带 progress 和 token_sink 回调）
    3. 循环从队列读取事件并 yield 给客户端
    4. Agent 完成后发送 final 事件

    【事件类型】
    - accepted：请求已接收
    - input_reviewed / vision_started / vision_completed / ...：各阶段进度
    - token：流式生成的文本增量
    - final：最终结果
    - error：处理失败

    【容错设计】
    - 客户端断开 → detached 模式（Agent 继续在后台完成）
    - Agent 异常 → 发送 error 事件
    - 队列空 → 等待 10 秒（asyncio.wait）或 Agent 完成
    - 每 10 秒发送 keep-alive 心跳

    :param request: FastAPI 请求对象
    :param command: 问诊命令对象
    :param agent: 问诊 Agent
    :return: SSE 事件流（AsyncIterator[str]）
    """
    # 进度事件缓冲区（Agent 回调写入，SSE 循环读取）
    queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    # 进度回调：Agent 每个阶段完成时调用
    async def progress(event: str, data: dict[str, Any]) -> None:
        """Agent 阶段完成回调，将进度事件放入队列。

        :param event: 阶段名称（input_reviewed / vision_started 等）
        :param data: 阶段数据（会被合并到 SSE 事件）
        """
        await queue.put(
            (
                event,
                {
                    "request_id": command.request_id,
                    "stage": event,
                    "message": _STAGE_MESSAGES.get(event, ""),
                    **data,
                },
            )
        )

    # Token 回调：Agent 每生成一个 token 调用一次
    async def token_sink(text: str) -> None:
        """流式 token 回调，将文本增量放入队列。

        :param text: 新生成的 token 文本
        """
        await queue.put(
            (
                "token",
                {
                    "request_id": command.request_id,
                    "stage": "token",
                    "message": "",
                    "text": text,
                },
            )
        )

    # 启动 Agent 后台任务（流式模式）
    task = asyncio.create_task(
        agent.run_stream(command, progress=progress, token_sink=token_sink)
    )

    # 发送"已接受"事件
    yield _sse(
        "accepted",
        {
            "request_id": command.request_id,
            "stage": "accepted",
            "message": "请求已接收",
        },
    )

    # detached 模式：客户端断开后 Agent 是否在后台继续
    detached = False
    try:
        # 主循环：Agent 未完成 或 队列还有事件
        while not task.done() or not queue.empty():
            # 客户端断开 → 退出循环（Agent 后台继续）
            if await request.is_disconnected():
                detached = True
                break

            # 队列有事件 → 立即发送
            if not queue.empty():
                event, data = queue.get_nowait()
                yield _sse(event, data)
                continue

            # 队列空 → 等待新事件或 Agent 完成
            queue_get = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {queue_get, task},
                timeout=10.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if queue_get in done:
                # 新事件到达 → 发送
                event, data = queue_get.result()
                yield _sse(event, data)
                continue

            # 超时 → 取消等待任务
            queue_get.cancel()
            with suppress(asyncio.CancelledError):
                await queue_get
            if not done:
                # Agent 未完成但无新事件 → 发送心跳
                yield ": keep-alive\n\n"

        # 客户端断开 → 不发送 final 事件
        if detached:
            return

        # Agent 完成 → 发送最终结果
        response = await task
        yield _sse(
            "final",
            {
                "request_id": command.request_id,
                "response": response.model_dump(mode="json"),
            },
        )
    except Exception:  # noqa: BLE001 - 异常通过 SSE 报告
        logger.exception("consult_stream_failed", extra={"request_id": command.request_id})
        yield _sse(
            "error",
            {
                "request_id": command.request_id,
                "error": {
                    "code": "STREAM_FAILED",
                    "message": "流式问诊中断，请稍后重试",
                    "retryable": True,
                },
            },
        )
    finally:
        # 确保后台任务异常被观察（避免 asyncio 警告）
        if not task.done():
            task.add_done_callback(lambda finished: finished.exception())


@router.post("/consult", response_model=ConsultResponse)
async def consult(
    request: Request,
    conversation_id: str = Form(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    ),
    text: str = Form(default="", max_length=4000),
    pet_info: str = Form(default=""),
    pet_ref: str | None = Form(default=None, max_length=64),
    images: list[UploadFile] = File(default=[]),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    auth: AuthContext = Depends(get_auth_context),
    agent: ConsultAgent = Depends(get_agent),
    rate: RateLimiter = Depends(get_rate_limiter),
    settings: Settings = Depends(get_app_settings),
) -> ConsultResponse:
    """问诊 API 端点（非流式）。

    【请求格式】multipart/form-data
    - conversation_id: 会话 ID（必填）
    - text: 问诊文本（可选，与 images 至少一项）
    - pet_info: 宠物信息 JSON（可选）
    - pet_ref: 指定宠物（可选）
    - images: 图片文件（可选，最多 3 张）
    - Idempotency-Key: 防重键（可选，Header）

    【执行路径】
    - consult_mq_enabled=true → _execute_via_queue()（队列化异步）
    - consult_mq_enabled=false → agent.run(command)（直连同步）

    :return: ConsultResponse（问诊结果）
    """
    command = await _prepare_command(
        request=request,
        conversation_id=conversation_id,
        text=text,
        pet_info=pet_info,
        pet_ref=pet_ref,
        images=images,
        idempotency_key=idempotency_key,
        auth=auth,
        rate=rate,
        settings=settings,
    )
    if settings.consult_mq_enabled:
        return await _execute_via_queue(
            request=request, command=command, agent=agent
        )
    return await agent.run(command)


@router.post("/consult/stream", response_class=StreamingResponse)
async def consult_stream(
    request: Request,
    conversation_id: str = Form(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    ),
    text: str = Form(default="", max_length=4000),
    pet_info: str = Form(default=""),
    pet_ref: str | None = Form(default=None, max_length=64),
    images: list[UploadFile] = File(default=[]),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    auth: AuthContext = Depends(get_auth_context),
    agent: ConsultAgent = Depends(get_agent),
    rate: RateLimiter = Depends(get_rate_limiter),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """问诊 API 端点（流式 SSE）。

    【响应格式】text/event-stream（Server-Sent Events）
    客户端可实时看到处理进度：
    - accepted → input_reviewed → vision_started → vision_completed →
      risk_assessed → answer_generated → medical_review_completed →
      output_review_completed → final

    【执行路径】
    - consult_mq_enabled=true → _queue_event_stream()（通过 Redis Stream 中转）
    - consult_mq_enabled=false → _consult_event_stream()（直连 asyncio.Queue）

    【HTTP Headers】
    - Cache-Control: no-cache（禁止缓存）
    - X-Accel-Buffering: no（Nginx 不缓冲）

    :return: StreamingResponse（SSE 事件流）
    """
    command = await _prepare_command(
        request=request,
        conversation_id=conversation_id,
        text=text,
        pet_info=pet_info,
        pet_ref=pet_ref,
        images=images,
        idempotency_key=idempotency_key,
        auth=auth,
        rate=rate,
        settings=settings,
    )
    if settings.consult_mq_enabled:
        # 队列模式：登记任务 + SSE 进度推送
        task_id = await _register_queue_task(
            request=request,
            command=command,
            enforce_capacity=True,
            stream_events=True,
        )
        return StreamingResponse(
            _queue_event_stream(
                request=request,
                command=command,
                agent=agent,
                task_id=task_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # 直连模式：Agent 直接推送 SSE 事件
    return StreamingResponse(
        _consult_event_stream(request=request, command=command, agent=agent),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )