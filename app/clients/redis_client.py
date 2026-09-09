"""Redis 连接封装（v5 §16：连接池 + 超时 1s 连接 / 2s 总）"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.core.config import Settings

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, settings: Settings, client: aioredis.Redis | None = None):
        self.s = settings
        self.client = client or aioredis.from_url(
            settings.redis_url,
            password=settings.redis_password or None,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )

    @property
    def available(self) -> bool:
        return self.client is not None

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis ping 失败: %s", exc)
            return False

    async def close(self) -> None:
        await self.client.aclose()
