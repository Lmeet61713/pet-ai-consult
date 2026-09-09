"""接口限流（v5 §22.3 用户级限流）

Redis INCR + 首请求 EXPIRE 窗口计数；mock 模式放行（本地测试用 fakeredis）。
限流服务异常时放行并告警（不阻断主链路）。
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis

from app.core.config import Settings
from app.utils.redis_lua import incr_with_ttl

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, settings: Settings, client: aioredis.Redis | None = None):
        self.s = settings
        self._client = client or aioredis.from_url(
            settings.redis_url,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    def _key(self, tenant_id: str, user_id: str, window_s: int) -> str:
        return (
            f"{self.s.redis_namespace}:rate_limit:"
            f"{tenant_id}:{user_id}:{window_s}"
        )

    async def allow(self, tenant_id: str, user_id: str) -> bool:
        if self.s.mock_mode or not self.s.rate_limit_enabled:
            return True
        key = self._key(tenant_id, user_id, self.s.rate_limit_window_seconds)
        try:
            n = await incr_with_ttl(
                self._client,
                key,
                ttl_seconds=self.s.rate_limit_window_seconds,
            )
            if n > self.s.rate_limit_max:
                logger.warning("限流命中: tenant=%s user=%s count=%d", tenant_id, user_id, n)
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - 限流失败不阻断服务
            logger.error("限流服务异常，放行: %s", exc)
            return True

    async def close(self) -> None:
        await self._client.aclose()
