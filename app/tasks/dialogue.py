"""consult_dialogue 效果存档（PG 版，v1.4 §7.1）。

- 异步写入：不阻塞响应关键路径；写失败仅告警；
- 90 天清理 + 用户删除接口。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.tasks.models import ConsultDialogue

logger = logging.getLogger(__name__)


class DialogueRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory

    async def write(self, record: dict) -> None:
        """写一行对话（按 request_id 幂等：已存在则更新）。

        只写入表字段（ts/rag_top_score 等表外字段自动丢弃）。
        """
        allowed = {c.name for c in ConsultDialogue.__table__.columns}
        row = {k: v for k, v in record.items() if k in allowed and k != "id"}
        try:
            async with self._session_factory() as session, session.begin():
                existing = await session.scalar(
                    select(ConsultDialogue.id).where(
                        ConsultDialogue.request_id == row["request_id"]
                    )
                )
                if existing is None:
                    session.add(ConsultDialogue(**row))
                else:
                    await session.execute(
                        ConsultDialogue.__table__.update()
                        .where(ConsultDialogue.id == existing)
                        .values(**{k: v for k, v in row.items() if k != "request_id"}),
                    )
        except Exception:  # noqa: BLE001 - 存档失败不影响主链路
            logger.warning("dialogue_pg_write_failed", exc_info=True)

    async def cleanup_older_than(self, days: int = 90) -> int:
        """清理过期存档；返回删除行数。"""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(ConsultDialogue).where(ConsultDialogue.created_at < cutoff)
            )
            return result.rowcount or 0

    async def delete_by_request_id(self, request_id: str) -> bool:
        """用户删除（合规要求）：返回是否删除。"""
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(ConsultDialogue).where(ConsultDialogue.request_id == request_id)
            )
            return bool(result.rowcount)

    async def get_by_request_id(self, request_id: str) -> ConsultDialogue | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ConsultDialogue).where(ConsultDialogue.request_id == request_id)
            )