"""Worker 任务执行（Phase 2 §4.3 分流）。

消费路径（v1.4 简化版）：Scheduler 从 PG 扫描 queued 任务执行；
RocketMQ PushConsumer 切换在新服务器环境就绪后（发布链已走 MQ，消费可双轨）。

分流：
- fast_path      → 卡片直答渲染（不调模型，<100ms）
- pre_answered   → 急症已同步返回过，仅补记账
- 其他           → 症状问诊（调用现有 ConsultAgent 完整流水线）

幂等：processing/completed 状态的任务跳过重复执行。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.tasks.models import ConsultTask
from app.tasks.service import TaskService

logger = logging.getLogger(__name__)


class ConsultWorker:
    """消费 queued 任务并分流执行。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_service: TaskService,
        *,
        fast_handler: Any | None = None,
        normal_handler: Any | None = None,
        worker_id: str = "consult-worker-1",
        max_retry: int = 3,
        metrics: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._task_service = task_service
        self._fast_handler = fast_handler
        self._normal_handler = normal_handler
        self.worker_id = worker_id
        self.max_retry = max_retry
        self.metrics = metrics

    async def process_one(self) -> bool:
        """领取一个 queued 任务并执行；返回是否处理了任务。"""
        import time as _time

        task = await self._claim_next()
        if task is None:
            return False
        t0 = _time.monotonic()
        await self._execute(task)
        if self.metrics is not None:
            self.metrics.observe(_time.monotonic() - t0)
        return True

    async def _claim_next(self) -> ConsultTask | None:
        """取最老 queued 任务（P0 优先）并标记 processing（幂等保护）。"""
        async with self._session_factory() as session, session.begin():
            task = await session.scalar(
                select(ConsultTask)
                .where(ConsultTask.status == "queued")
                # P0 < P1 字典序，asc 保证 P0 优先（未来 P2/P3 同理）
                .order_by(ConsultTask.priority.asc(), ConsultTask.created_at)
                .limit(1)
                .with_for_update(skip_locked=True),
            )
            if task is None:
                return None
            task.status = "processing"
            task.worker_id = self.worker_id
            return task

    async def _execute(self, task: ConsultTask) -> None:
        try:
            if task.pre_answered:
                # 急症已同步返回固定模板：仅补记账，跳过处理（v1.4 §4.3）
                await self._task_service.complete(task.id, worker_id=self.worker_id)
                if self.metrics is not None:
                    self.metrics.record("processed")
                logger.info("worker_pre_answered_skipped", extra={"task_id": task.id})
                return
            if task.fast_path:
                await self._run_fast(task)
                if self.metrics is not None:
                    self.metrics.record("fast")
                return
            await self._run_normal(task)
            if self.metrics is not None:
                self.metrics.record("processed")
        except Exception as exc:  # noqa: BLE001 - 失败按重试上限回队列或死信
            logger.warning("worker_task_failed", extra={"task_id": task.id, "error": str(exc)[:200]})
            final_status = await self._task_service.requeue_or_dead_letter(
                task.id, str(exc)[:300], max_retry=self.max_retry, worker_id=self.worker_id
            )
            if self.metrics is not None:
                self.metrics.record("failed" if final_status == "failed" else "requeued" if final_status == "queued" else "dead_letter")
            if final_status == "dead_letter":
                logger.error(
                    "worker_task_dead_lettered",
                    extra={"task_id": task.id, "retry_count": task.retry_count + 1},
                )

    async def _run_fast(self, task: ConsultTask) -> None:
        """卡片直答：payload 里的卡片内容直接渲染（无模型依赖）。"""
        if self._fast_handler is None:
            raise RuntimeError("fast_handler 未配置")
        await self._fast_handler(task)
        await self._task_service.complete(task.id, worker_id=self.worker_id)

    async def _run_normal(self, task: ConsultTask) -> None:
        """症状问诊：走现有 ConsultAgent 流水线。"""
        if self._normal_handler is None:
            raise RuntimeError("normal_handler 未配置")
        await self._normal_handler(task)
        await self._task_service.complete(task.id, worker_id=self.worker_id)


class WorkerLoop:
    """Worker 主循环：轮询 queued 任务，空闲等待，可优雅停止。"""

    def __init__(
        self, worker: ConsultWorker, *, poll_seconds: float = 0.2, idle_sleep: float = 0.5
    ) -> None:
        self._worker = worker
        self._poll_seconds = poll_seconds
        self._idle_sleep = idle_sleep
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                processed = await self._worker.process_one()
            except Exception:  # noqa: BLE001 - 循环不得中断
                logger.exception("worker_loop_error")
                processed = False
            if processed:
                await asyncio.sleep(self._poll_seconds)
            else:
                await asyncio.sleep(self._idle_sleep)
