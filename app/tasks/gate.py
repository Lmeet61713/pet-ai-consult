"""直答同步车道并发闸门（v1.4：防直答溢出打爆 API 进程）。

- Redis INCR/DECR 原子计数：并发超过上限 → 溢出到队列车道；
- Redis 不可用 → 闸门失效直行（与限流器降级策略一致，不阻断直答）；
- 溢出与同步计数进 TaskMetrics，queue_metrics 日志可观测。
"""
from __future__ import annotations

import logging

from app.clients.redis_client import RedisClient

logger = logging.getLogger(__name__)


class FastLaneGate:
    def __init__(self, redis: RedisClient, namespace: str, max_concurrent: int = 10):
        self._redis = redis
        self._key = f"{namespace}:fast_lane_active"
        self._max = max_concurrent

    async def acquire(self) -> bool:
        """尝试占用一个直答同步额度；返回是否获得。"""
        try:
            current = await self._redis.client.incr(self._key)
            if current > self._max:
                await self._redis.client.decr(self._key)
                return False
            return True
        except Exception:  # noqa: BLE001 - Redis 不可用 → 闸门失效直行
            logger.warning("fast_lane_gate_unavailable", exc_info=True)
            return True

    async def release(self) -> None:
        try:
            await self._redis.client.decr(self._key)
        except Exception:  # noqa: BLE001
            logger.warning("fast_lane_gate_release_failed", exc_info=True)