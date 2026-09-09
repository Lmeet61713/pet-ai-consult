"""会话持久化（v5 §15：meta Hash / turns List / summary 三 key）

- TTL 7 天，每次写入刷新
- 保留最近 MAX_CONVERSATION_TURNS 轮（旧轮次截断，超长后由服务层生成摘要）
- 原始图片不进入 Redis（只存 image_findings 结构化结果）
- 会话锁：SET NX EX + owner token（V1.1 P0-2）
  - 租约至少覆盖总 deadline + 15s（等待时间与租约分开）
  - 释放必须比较 owner token（Lua），避免 A 删除 B 后来获得的新锁
  - Redis 异常统一转 RedisUnavailable，由 agent 降级为无锁单轮
"""
from __future__ import annotations

import logging
import secrets

import redis.asyncio as aioredis
import redis.exceptions as redis_exc

from app.core.config import Settings
from app.core.exceptions import ConversationConflictError, RedisUnavailable
from app.schemas.conversation import (
    ConversationKey,
    ConversationMeta,
    ConversationSnapshot,
    ConversationTurn,
)
from app.utils.redis_lua import compare_delete
from app.utils.time import utc_now_iso

logger = logging.getLogger(__name__)

class ConversationRepository:
    def __init__(self, settings: Settings, client: aioredis.Redis):
        self.s = settings
        self._client = client

    # ------------------------------------------------------------ 读写

    async def load(self, key: ConversationKey) -> ConversationSnapshot:
        try:
            meta_raw = await self._client.hgetall(key.meta_key)
            turns_raw = await self._client.lrange(key.turns_key, 0, -1)
            summary = await self._client.get(key.summary_key) or ""
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("会话历史不可用，已降级为无记忆单轮") from exc

        turns: list[ConversationTurn] = []
        for t in turns_raw:
            try:
                turns.append(ConversationTurn.model_validate_json(t))
            except Exception:  # noqa: BLE001 - 单轮损坏跳过，不阻塞
                logger.warning("会话 %s 单轮反序列化失败，跳过", key.conversation_id)

        meta = ConversationMeta.model_validate(meta_raw) if meta_raw else ConversationMeta()
        return ConversationSnapshot(key=key, meta=meta, turns=turns, summary=summary)

    async def append(self, key: ConversationKey, turn: ConversationTurn) -> None:
        turn_id = turn.turn_id or "turn_" + secrets.token_hex(4)
        turn = turn.model_copy(update={"turn_id": turn_id})
        pipe = self._client.pipeline()
        pipe.rpush(key.turns_key, turn.model_dump_json())
        pipe.ltrim(key.turns_key, -self.s.max_conversation_turns, -1)  # 保留最近 N 轮
        now = utc_now_iso()
        pipe.hsetnx(key.meta_key, "created_at", now)  # 仅首次写入
        pipe.hset(
            key.meta_key,
            mapping={
                "updated_at": now,
                "schema_version": "2",
                "model_version": turn.model_versions.get("vision_model", ""),
            },
        )
        pipe.expire(key.turns_key, self.s.redis_ttl_seconds)
        pipe.expire(key.meta_key, self.s.redis_ttl_seconds)
        pipe.expire(key.summary_key, self.s.redis_ttl_seconds)
        await pipe.execute()
        count = await self._client.llen(key.turns_key)
        await self._client.hset(key.meta_key, mapping={"turn_count": count})

    async def save_summary(self, key: ConversationKey, summary: str) -> None:
        await self._client.set(key.summary_key, summary, ex=self.s.redis_ttl_seconds)

    async def delete(self, key: ConversationKey) -> None:
        await self._client.delete(key.meta_key, key.turns_key, key.summary_key)

    # ------------------------------------------------------------ 锁（V1.1 P0-2）

    async def acquire_lock(
        self,
        key: ConversationKey,
        *,
        owner_token: str,
        lease_seconds: float | None = None,
    ) -> bool:
        """尝试获取同会话串行锁；owner_token 唯一标识本次请求。

        租约必须覆盖整个处理窗口（等待时间单独由 wait_for_lock 控制）。
        """
        try:
            effective_lease = max(
                lease_seconds or self.s.conversation_lock_lease_seconds,
                self.s.consult_total_timeout_seconds + 15.0,
            )
            ok = await self._client.set(
                key.lock_key, owner_token, nx=True, ex=int(effective_lease)
            )
            return bool(ok)
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("会话锁不可用，已降级为无锁单轮") from exc

    async def release_lock(self, key: ConversationKey, *, owner_token: str) -> None:
        """释放锁：比较 owner token 后删除（Lua 原子，不删别人的锁）。"""
        try:
            await compare_delete(self._client, key.lock_key, owner_token)
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("会话锁释放失败（TTL 兜底自动过期）") from exc

    async def wait_for_lock(
        self,
        key: ConversationKey,
        *,
        owner_token: str,
        timeout_s: float | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        """等待锁释放（轮询，等待超时与租约无关）；超时抛 ConversationConflictError。"""
        import asyncio

        loop = asyncio.get_event_loop()
        effective_wait = timeout_s or self.s.conversation_lock_wait_seconds
        effective_lease = max(
            lease_seconds or self.s.conversation_lock_lease_seconds,
            self.s.consult_total_timeout_seconds + 15.0,
        )
        deadline = loop.time() + effective_wait
        while loop.time() < deadline:
            try:
                if await self.acquire_lock(
                    key, owner_token=owner_token, lease_seconds=effective_lease
                ):
                    return True
            except RedisUnavailable:
                raise
            await asyncio.sleep(0.05)
        raise ConversationConflictError("会话正在处理中，请稍后重试")
