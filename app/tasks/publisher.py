"""Outbox 扫描器：发布失败补发（Phase 2 §4.4）。

定时扫描 consult_outbox 中未发布的记录 → 发布 RocketMQ → 标记 published。
发布成功后任务状态 registered → queued。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.tasks.mq import ConsultMessage, ConsultMqPublisherProto, MessagePublishError
from app.tasks.models import ConsultOutbox
from app.tasks.service import TaskService

logger = logging.getLogger(__name__)


class OutboxScanner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: ConsultMqPublisherProto,
        task_service: TaskService,
        *,
        republish_after_seconds: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._task_service = task_service
        self._republish_after_seconds = republish_after_seconds
        # API 请求会立即触发一次扫描，后台循环也会定时扫描。
        # 同一进程内必须串行，否则同一 outbox 可能被重复发布。
        self._scan_lock = asyncio.Lock()

    async def scan_once(self, limit: int = 50) -> int:
        async with self._scan_lock:
            return await self._scan_once_unlocked(limit)

    async def _scan_once_unlocked(self, limit: int = 50) -> int:
        """扫描未发布 outbox（含超时待重发）并发布；返回发布成功条数。"""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._republish_after_seconds)
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ConsultOutbox.id, ConsultOutbox.task_id, ConsultOutbox.topic, ConsultOutbox.tag, ConsultOutbox.payload)
                        .where(
                            ConsultOutbox.published.is_(False),
                            or_(
                                ConsultOutbox.published_at.is_(None),
                                ConsultOutbox.published_at < cutoff,
                            ),
                        )
                        .order_by(ConsultOutbox.created_at)
                        .limit(limit)
                    )
                ).all()
            )
        published = 0
        for outbox_id, task_id, topic, tag, payload in rows:
            try:
                message = ConsultMessage.model_validate(payload)
                await self._publisher.publish(message)
            except (MessagePublishError, ValueError) as exc:
                logger.warning(
                    "outbox_publish_failed",
                    extra={"outbox_id": outbox_id, "task_id": task_id, "error": str(exc)[:200]},
                )
                continue
            async with self._session_factory() as session, session.begin():
                outbox = await session.scalar(
                    select(ConsultOutbox).where(ConsultOutbox.id == outbox_id).with_for_update()
                )
                if outbox is not None:
                    outbox.published = True
                    outbox.published_at = datetime.now(UTC)
            await self._task_service.mark_queued(task_id)
            published += 1
        return published
