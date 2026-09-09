"""队列模式 SSE 阶段事件总线（Redis Stream）。"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskProgressStream:
    def __init__(self, redis_client, namespace: str, *, ttl_seconds: int = 300):
        self._redis = redis_client
        self._namespace = namespace
        self._ttl_seconds = ttl_seconds

    def key(self, request_id: str) -> str:
        return f"{self._namespace}:sse:{request_id}"

    async def publish(self, request_id: str, event: str, data: dict[str, Any]) -> None:
        key = self.key(request_id)
        try:
            await self._redis.xadd(
                key,
                {
                    "event": event,
                    "data": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                },
                maxlen=200,
                approximate=True,
            )
            await self._redis.expire(key, self._ttl_seconds)
        except Exception:  # noqa: BLE001 - 进度事件失败不得影响问诊主链路
            logger.warning(
                "task_progress_publish_failed",
                extra={"request_id": request_id, "event": event},
                exc_info=True,
            )

    async def read(
        self,
        request_id: str,
        last_id: str,
        *,
        block_ms: int = 1000,
        count: int = 20,
    ) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        rows = await self._redis.xread(
            {self.key(request_id): last_id},
            block=block_ms,
            count=count,
        )
        events: list[tuple[str, dict[str, Any]]] = []
        newest = last_id
        for _, entries in rows:
            for entry_id, fields in entries:
                newest = entry_id
                event = str(fields.get("event") or "progress")
                try:
                    data = json.loads(fields.get("data") or "{}")
                except (TypeError, json.JSONDecodeError):
                    data = {}
                events.append((event, data))
        return newest, events
