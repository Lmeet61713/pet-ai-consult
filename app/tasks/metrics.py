"""Phase 2.7 最小监控：任务指标（内存计数 + 定期日志）。

Prometheus/Grafana 在新服务器接入；当前克隆实例用结构化日志暴露，
queue_depth / oldest_wait 由 QueueMonitor 定期扫描 PG。
"""
from __future__ import annotations

import logging
import statistics
import time
from collections import deque

from sqlalchemy import func, select

from app.tasks.models import ConsultTask
from app.tasks.service import ACTIVE_STATUSES, TaskService

logger = logging.getLogger(__name__)


class TaskMetrics:
    """Worker 侧指标：计数 + 处理时长滚动窗口（p50/p95/p99）。"""

    def __init__(self, window_size: int = 1000):
        self.processed_total = 0
        self.failed_total = 0
        self.requeued_total = 0
        self.dead_letter_total = 0
        self.cancelled_total = 0
        self.fast_total = 0
        self.fast_sync_total = 0
        self.fast_overflow_total = 0
        self._seconds: deque[float] = deque(maxlen=window_size)

    def observe(self, seconds: float) -> None:
        self._seconds.append(seconds)

    def record(self, outcome: str) -> None:
        if outcome == "processed":
            self.processed_total += 1
        elif outcome == "failed":
            self.failed_total += 1
        elif outcome == "requeued":
            self.requeued_total += 1
        elif outcome == "dead_letter":
            self.dead_letter_total += 1
        elif outcome == "cancelled":
            self.cancelled_total += 1
        elif outcome == "fast":
            self.fast_total += 1
        elif outcome == "fast_sync":
            self.fast_sync_total += 1
        elif outcome == "fast_overflow":
            self.fast_overflow_total += 1

    def snapshot(self) -> dict:
        if not self._seconds:
            p50 = p95 = p99 = 0.0
        else:
            ordered = sorted(self._seconds)
            p50 = statistics.median(ordered)
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
            p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
        return {
            "processed_total": self.processed_total,
            "failed_total": self.failed_total,
            "requeued_total": self.requeued_total,
            "dead_letter_total": self.dead_letter_total,
            "cancelled_total": self.cancelled_total,
            "fast_total": self.fast_total,
            "fast_sync_total": self.fast_sync_total,
            "fast_overflow_total": self.fast_overflow_total,
            "processing_p50_s": round(p50, 3),
            "processing_p95_s": round(p95, 3),
            "processing_p99_s": round(p99, 3),
        }


class QueueMonitor:
    """定期扫描 PG：队列深度/最老等待/状态分布 → 结构化日志。"""

    def __init__(
        self,
        session_factory,
        metrics: TaskMetrics,
        task_service: TaskService | None = None,
    ):
        self._session_factory = session_factory
        self._metrics = metrics
        self._task_service = task_service
        self._last_processed = 0
        self._last_ts = time.monotonic()

    async def log_once(self) -> None:
        async with self._session_factory() as session:
            status_counts = dict(
                (
                    await session.execute(
                        select(ConsultTask.status, func.count()).group_by(ConsultTask.status)
                    )
                ).all()
            )
            queue_depth = status_counts.get("registered", 0) + status_counts.get("queued", 0)
            oldest_row = await session.execute(
                select(ConsultTask.created_at)
                .where(ConsultTask.status.in_(["registered", "queued"]))
                .order_by(ConsultTask.created_at)
                .limit(1),
            )
            oldest = oldest_row.scalar()
            active_by_kind = dict(
                (
                    await session.execute(
                        select(ConsultTask.task_kind, func.count())
                        .where(ConsultTask.status.in_(ACTIVE_STATUSES))
                        .group_by(ConsultTask.task_kind)
                    )
                ).all()
            )
            image_slots = (
                await session.scalar(
                    select(func.coalesce(func.sum(ConsultTask.image_count), 0)).where(
                        ConsultTask.status.in_(ACTIVE_STATUSES),
                        ConsultTask.task_kind == "image",
                    )
                )
            ) or 0
            oldest_by_kind = dict(
                (
                    await session.execute(
                        select(ConsultTask.task_kind, func.min(ConsultTask.created_at))
                        .where(ConsultTask.status.in_(["registered", "queued"]))
                        .group_by(ConsultTask.task_kind)
                    )
                ).all()
            )
        oldest_wait_s = 0.0
        if oldest is not None:
            oldest_wait_s = max(0.0, time.time() - oldest.timestamp())
        snapshot = self._metrics.snapshot()
        wait_by_kind = {
            kind: round(max(0.0, time.time() - created.timestamp()), 1)
            for kind, created in oldest_by_kind.items()
            if created is not None
        }
        now = time.monotonic()
        rate = (snapshot["processed_total"] - self._last_processed) / max(now - self._last_ts, 1.0)
        self._last_processed = snapshot["processed_total"]
        self._last_ts = now
        logger.info(
            "queue_metrics",
            extra={
                "queue_depth": queue_depth,
                "oldest_wait_s": round(oldest_wait_s, 1),
                "status_counts": status_counts,
                "active_text": active_by_kind.get("text", 0),
                "active_image": active_by_kind.get("image", 0),
                "active_image_slots": image_slots,
                "oldest_wait_by_kind_s": wait_by_kind,
                "admission_rejected": (
                    self._task_service.admission_snapshot()
                    if self._task_service is not None
                    else {}
                ),
                "processed_rate_per_s": round(rate, 2),
                **snapshot,
            },
        )
