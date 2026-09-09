from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.exceptions import QueueBusyError
from app.tasks.models import Base, ConsultTask
from app.tasks.progress import TaskProgressStream
from app.tasks.service import TaskService


@pytest.mark.asyncio
async def test_capacity_counts_registered_and_rejects_next_task():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = TaskService(factory)

    for index in range(2):
        await service.register_task(
            request_id=f"r{index}",
            tenant_id="t",
            user_id="u",
            conversation_id=f"c{index}",
            payload={},
            max_active=2,
        )

    assert await service.count_active() == 2
    with pytest.raises(QueueBusyError):
        await service.register_task(
            request_id="r3",
            tenant_id="t",
            user_id="u",
            conversation_id="c3",
            payload={},
            max_active=2,
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_registered_task_is_expired_before_capacity_count():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = TaskService(factory)
    task_id = await service.register_task(
        request_id="old",
        tenant_id="t",
        user_id="u",
        conversation_id="c",
        payload={},
    )
    async with factory() as session, session.begin():
        task = await session.scalar(select(ConsultTask).where(ConsultTask.id == task_id))
        task.updated_at = datetime.now(UTC) - timedelta(seconds=300)

    expired = await service.expire_stale_active(120)
    task = await service.get_task(task_id)
    assert expired == 1
    assert task.status == "timeout"
    assert await service.count_active() == 0
    await engine.dispose()


class _FakeRedis:
    def __init__(self):
        self.rows = []
        self.ttl = None

    async def xadd(self, key, fields, **kwargs):
        self.rows.append(("1-0", fields))
        return "1-0"

    async def expire(self, key, ttl):
        self.ttl = ttl

    async def xread(self, streams, **kwargs):
        return [("stream", self.rows)] if self.rows else []


@pytest.mark.asyncio
async def test_progress_stream_round_trip():
    redis = _FakeRedis()
    stream = TaskProgressStream(redis, "pet_consult:test", ttl_seconds=300)
    await stream.publish("r1", "vision_started", {"stage": "vision_started"})
    last_id, events = await stream.read("r1", "0-0")
    assert last_id == "1-0"
    assert events == [("vision_started", {"stage": "vision_started"})]
    assert redis.ttl == 300
