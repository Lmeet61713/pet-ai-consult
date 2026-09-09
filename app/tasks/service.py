"""任务服务：登记（task+outbox 同事务）与状态流转（事件审计）。

Phase 2 §4.4：先写库后发布；状态变更写 consult_task_event 供监控。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.tasks.models import ConsultOutbox, ConsultTask, ConsultTaskEvent
from app.core.exceptions import QueueBusyError

logger = logging.getLogger(__name__)

# 任务终态（不再流转）
TERMINAL_STATUSES = {"completed", "failed", "timeout", "cancelled", "dead_letter"}
ACTIVE_STATUSES = {"registered", "queued", "scheduled", "processing"}
_ADMISSION_LOCK_KEY = 7_217_201


class TaskService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory
        self._admission_rejections: Counter[str] = Counter()

    def admission_snapshot(self) -> dict[str, int]:
        """Process-local rejection counters for periodic structured metrics."""
        return dict(self._admission_rejections)

    async def count_active(self) -> int:
        """活动任务数（registered/queued/scheduled/processing）。"""
        async with self._session_factory() as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(ConsultTask)
                    .where(ConsultTask.status.in_(ACTIVE_STATUSES))
                )
            ) or 0

    async def register_task(
        self,
        *,
        request_id: str,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        priority: str = "P1",
        fast_path: bool = False,
        pre_answered: bool = False,
        payload: dict | None = None,
        topic: str = "consult-tasks",
        max_active: int = 0,
        task_kind: str = "text",
        image_count: int = 0,
        text_max_active: int = 0,
        image_max_active: int = 0,
        image_max_active_slots: int = 0,
        stale_after_seconds: int = 120,
    ) -> int:
        """登记任务 + outbox（同事务）。返回 task_id。

        request_id 唯一约束兜底幂等：重复登记返回已有任务 id，不重复发布。
        """
        payload = payload or {}
        if task_kind not in {"text", "image"}:
            raise ValueError("task_kind must be 'text' or 'image'")
        if image_count < 0 or image_count > 3:
            raise ValueError("image_count must be between 0 and 3")
        if task_kind == "text" and image_count != 0:
            raise ValueError("text tasks cannot consume image slots")
        if task_kind == "image" and image_count < 1:
            raise ValueError("image tasks must consume at least one image slot")
        classified_limits = text_max_active > 0 or image_max_active > 0
        if classified_limits and not (text_max_active > 0 and image_max_active > 0):
            raise ValueError("classified admission limits must be configured together")
        capacity_enabled = max_active > 0 or classified_limits
        async with self._session_factory() as session, session.begin():
            # PostgreSQL 事务级 advisory lock 将“统计容量+登记”串成一个原子准入操作。
            # 同一 request_id 的幂等查询也在锁内，避免并发重复登记。
            if capacity_enabled and session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _ADMISSION_LOCK_KEY},
                )
            existing = await session.scalar(
                select(ConsultTask.id).where(ConsultTask.request_id == request_id)
            )
            if existing is not None:
                return existing
            if max_active > 0:
                await self._expire_stale_in_session(session, stale_after_seconds)
                active = (
                    await session.scalar(
                        select(func.count())
                        .select_from(ConsultTask)
                        .where(ConsultTask.status.in_(ACTIVE_STATUSES))
                    )
                ) or 0
                if active >= max_active:
                    self._admission_rejections["global"] += 1
                    raise QueueBusyError("系统繁忙，请稍后重试")
            if classified_limits:
                await self._expire_stale_in_session(session, stale_after_seconds)
                kind_active = (
                    await session.scalar(
                        select(func.count())
                        .select_from(ConsultTask)
                        .where(
                            ConsultTask.status.in_(ACTIVE_STATUSES),
                            ConsultTask.task_kind == task_kind,
                        )
                    )
                ) or 0
                kind_limit = text_max_active if task_kind == "text" else image_max_active
                if kind_active >= kind_limit:
                    self._admission_rejections[f"{task_kind}_active"] += 1
                    raise QueueBusyError(f"{task_kind} 请求繁忙，请稍后重试")
                if task_kind == "image" and image_max_active_slots > 0:
                    active_slots = (
                        await session.scalar(
                            select(func.coalesce(func.sum(ConsultTask.image_count), 0)).where(
                                ConsultTask.status.in_(ACTIVE_STATUSES),
                                ConsultTask.task_kind == "image",
                            )
                        )
                    ) or 0
                    if active_slots + image_count > image_max_active_slots:
                        self._admission_rejections["image_slots"] += 1
                        raise QueueBusyError("图片分析槽位繁忙，请稍后重试")
            task = ConsultTask(
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                conversation_id=conversation_id,
                priority=priority,
                fast_path=fast_path,
                pre_answered=pre_answered,
                status="registered",
                task_kind=task_kind,
                image_count=image_count,
                payload=payload,
            )
            session.add(task)
            await session.flush()  # 取 task.id
            session.add(
                ConsultOutbox(
                    task_id=task.id,
                    topic=topic,
                    tag=priority,
                    payload={
                        "task_id": task.id,
                        "request_id": request_id,
                        "priority": priority,
                        "fast_path": fast_path,
                        "pre_answered": pre_answered,
                        "task_kind": task_kind,
                        "image_count": image_count,
                    },
                )
            )
            session.add(
                ConsultTaskEvent(task_id=task.id, from_status="", to_status="registered")
            )
            return task.id

    async def expire_stale_active(self, stale_after_seconds: int = 120) -> int:
        """回收异常遗留的活动任务，防止永久占满原子准入槽位。"""
        async with self._session_factory() as session, session.begin():
            return await self._expire_stale_in_session(session, stale_after_seconds)

    @staticmethod
    async def _expire_stale_in_session(
        session: AsyncSession, stale_after_seconds: int
    ) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        tasks = list(
            (
                await session.scalars(
                    select(ConsultTask)
                    .where(
                        ConsultTask.status.in_(ACTIVE_STATUSES),
                        ConsultTask.updated_at < cutoff,
                    )
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for task in tasks:
            previous = task.status
            task.status = "timeout"
            session.add(
                ConsultTaskEvent(
                    task_id=task.id,
                    from_status=previous,
                    to_status="timeout",
                    worker_id=task.worker_id,
                    reason=f"active_stale_over_{stale_after_seconds}s",
                )
            )
        return len(tasks)

    async def transition(
        self,
        task_id: int,
        to_status: str,
        *,
        worker_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """状态流转：更新任务 + 写事件（同事务）。终态后不再流转。"""
        async with self._session_factory() as session, session.begin():
            task = await session.scalar(
                select(ConsultTask).where(ConsultTask.id == task_id).with_for_update()
            )
            if task is None:
                logger.warning("task_transition_missing_task", extra={"task_id": task_id})
                return
            if task.status in TERMINAL_STATUSES:
                logger.warning(
                    "task_transition_from_terminal",
                    extra={"task_id": task_id, "from": task.status, "to": to_status},
                )
                return
            from_status = task.status
            task.status = to_status
            task.worker_id = worker_id or task.worker_id
            session.add(
                ConsultTaskEvent(
                    task_id=task_id,
                    from_status=from_status,
                    to_status=to_status,
                    worker_id=worker_id,
                    reason=reason,
                )
            )

    async def mark_queued(self, task_id: int, *, worker_id: str | None = None) -> None:
        """仅允许 registered → queued，避免重复发布把 processing 任务退回队列。"""
        async with self._session_factory() as session, session.begin():
            task = await session.scalar(
                select(ConsultTask).where(ConsultTask.id == task_id).with_for_update()
            )
            if task is None:
                logger.warning("task_queue_missing_task", extra={"task_id": task_id})
                return
            if task.status != "registered":
                logger.info(
                    "task_queue_transition_skipped",
                    extra={"task_id": task_id, "from": task.status},
                )
                return
            task.status = "queued"
            task.worker_id = worker_id or task.worker_id
            session.add(
                ConsultTaskEvent(
                    task_id=task_id,
                    from_status="registered",
                    to_status="queued",
                    worker_id=worker_id,
                )
            )

    async def mark_processing(self, task_id: int, *, worker_id: str | None = None) -> None:
        await self.transition(task_id, "processing", worker_id=worker_id)

    async def complete(self, task_id: int, *, worker_id: str | None = None) -> None:
        await self.transition(task_id, "completed", worker_id=worker_id)

    async def fail(
        self, task_id: int, reason: str, *, worker_id: str | None = None
    ) -> None:
        await self.transition(task_id, "failed", worker_id=worker_id, reason=reason)

    async def cancel(
        self, task_id: int, reason: str, *, worker_id: str | None = None
    ) -> None:
        await self.transition(task_id, "cancelled", worker_id=worker_id, reason=reason)

    async def requeue_or_dead_letter(
        self, task_id: int, reason: str, *, max_retry: int = 3, worker_id: str | None = None
    ) -> str:
        """失败处置：重试次数未超限 → 回 queued；超限 → dead_letter（v1.4 §4.4）。

        返回最终状态（queued / dead_letter）。
        """
        async with self._session_factory() as session, session.begin():
            task = await session.scalar(
                select(ConsultTask).where(ConsultTask.id == task_id).with_for_update()
            )
            if task is None or task.status in TERMINAL_STATUSES:
                return task.status if task else "missing"
            task.retry_count += 1
            if task.retry_count >= max_retry:
                to_status = "dead_letter"
            else:
                to_status = "queued"
            from_status = task.status
            task.status = to_status
            session.add(
                ConsultTaskEvent(
                    task_id=task_id,
                    from_status=from_status,
                    to_status=to_status,
                    worker_id=worker_id,
                    reason=f"retry#{task.retry_count}: {reason[:200]}",
                )
            )
            return to_status

    async def cancel_by_request_id(self, request_id: str, reason: str) -> bool:
        """断线取消：按 request_id 标记 cancelled（Worker 生成循环检查）。"""
        async with self._session_factory() as session, session.begin():
            task = await session.scalar(
                select(ConsultTask).where(ConsultTask.request_id == request_id)
            )
            if task is None:
                return False
            await self.cancel(task.id, reason)
            return True

    async def save_result(self, task_id: int, result: dict) -> None:
        """Worker 写回结果（与 complete 同事务）。"""
        async with self._session_factory() as session, session.begin():
            task = await session.scalar(
                select(ConsultTask).where(ConsultTask.id == task_id).with_for_update()
            )
            if task is None or task.status in TERMINAL_STATUSES:
                return
            task.result_json = result
            task.status = "completed"
            session.add(
                ConsultTaskEvent(
                    task_id=task_id,
                    from_status="processing",
                    to_status="completed",
                    worker_id=task.worker_id,
                )
            )

    async def wait_result(
        self, request_id: str, *, timeout_seconds: float = 45.0, poll_seconds: float = 0.2
    ) -> dict | None:
        """轮询任务结果（API 等待 Worker 完成）；超时返回 None。"""
        import asyncio as _asyncio
        from datetime import UTC, datetime as _dt

        deadline_ts = _dt.now(UTC).timestamp() + timeout_seconds
        while True:
            async with self._session_factory() as session:
                task = await session.scalar(
                    select(ConsultTask).where(ConsultTask.request_id == request_id)
                )
                if task is not None:
                    if task.status == "completed" and task.result_json:
                        return task.result_json
                    if task.status in ("failed", "dead_letter", "cancelled", "timeout"):
                        return {
                            "request_id": task.request_id,
                            "conversation_id": task.conversation_id,
                            "status": "error",
                            "retryable": True,
                            "error": {
                                "code": "TASK_" + task.status.upper(),
                                "message": "任务处理失败（" + task.status + "）",
                                "retryable": True,
                            },
                        }
            if _dt.now(UTC).timestamp() >= deadline_ts:
                return None
            await _asyncio.sleep(poll_seconds)

    async def get_task(self, task_id: int) -> ConsultTask | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ConsultTask).where(ConsultTask.id == task_id)
            )

    async def get_by_request_id(self, request_id: str) -> ConsultTask | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ConsultTask).where(ConsultTask.request_id == request_id)
            )
