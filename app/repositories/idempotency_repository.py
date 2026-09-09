"""幂等结果存储（v5 §10.1 Idempotency-Key / §15.1 idempotency key）

V1.1 P1-4：原子占位含请求指纹+owner、只缓存确定性终态、owner 校验替换/删除；
V1.1 P0-2：Redis 异常统一转 RedisUnavailable（agent 跳过幂等，不阻断请求）。
"""
from __future__ import annotations

import logging

import redis.asyncio as aioredis
import redis.exceptions as redis_exc

from app.core.config import Settings
from app.core.exceptions import IdempotencyConflictError, RedisUnavailable
from app.utils.redis_lua import compare_delete, compare_set

logger = logging.getLogger(__name__)

_TTL_SECONDS = 86400  # 1 天
_IN_PROGRESS_TTL_SECONDS = 120  # 占位短 TTL：进程崩溃后自动释放（P1-4）


class IdempotencyRepository:
    def __init__(self, settings: Settings, client: aioredis.Redis):
        self.s = settings
        self._client = client

    def _key(self, tenant_id: str, user_id: str, idempotency_key: str) -> str:
        return (
            f"{self.s.redis_namespace}:idempotency:"
            f"{tenant_id}:{user_id}:{idempotency_key}"
        )

    @staticmethod
    def _claim_value(request_hash: str, owner: str) -> str:
        return f"in_progress:{request_hash}:{owner}"

    @staticmethod
    def _result_value(request_hash: str, result_json: str) -> str:
        return f"result:{request_hash}:{result_json}"

    async def try_claim(
        self,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        owner: str,
    ) -> bool:
        """原子占位；value 同时绑定请求指纹和 owner。

        返回 True=本请求负责执行，False=已有请求在处理。
        """
        key = self._key(tenant_id, user_id, idempotency_key)
        claim = self._claim_value(request_hash, owner)
        try:
            ok = await self._client.set(
                key, claim, nx=True, ex=_IN_PROGRESS_TTL_SECONDS
            )
            return bool(ok)
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("幂等占位不可用，跳过幂等继续处理") from exc

    async def store_result(
        self,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        owner: str,
        result_json: str,
    ) -> None:
        """仅占位持有者可原子写入确定性终态。"""
        key = self._key(tenant_id, user_id, idempotency_key)
        expected = self._claim_value(request_hash, owner)
        value = self._result_value(request_hash, result_json)
        try:
            stored = await compare_set(
                self._client, key, expected, value, ttl_seconds=_TTL_SECONDS
            )
            if not stored:
                raise RedisUnavailable("幂等占位已失效，结果未缓存")
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("幂等结果写入失败（不影响本轮返回）") from exc

    async def get_result(
        self, tenant_id: str, user_id: str, idempotency_key: str, request_hash: str
    ) -> str | None:
        key = self._key(tenant_id, user_id, idempotency_key)
        try:
            raw = await self._client.get(key)
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("幂等读取不可用，跳过幂等继续处理") from exc
        if not raw:
            return None
        if raw.startswith("in_progress:"):
            _, stored_hash, _ = raw.split(":", 2)
            if stored_hash != request_hash:
                raise IdempotencyConflictError("Idempotency-Key 已用于其他请求内容")
            return None
        if raw.startswith("result:"):
            _, stored_hash, result_json = raw.split(":", 2)
            if stored_hash != request_hash:
                raise IdempotencyConflictError("Idempotency-Key 已用于其他请求内容")
            return result_json
        # 旧版本缓存没有请求指纹，不能安全判断是否为同一请求。
        raise IdempotencyConflictError("Idempotency-Key 命中旧格式缓存，请更换后重试")

    async def release_claim(
        self,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        owner: str,
    ) -> None:
        """失败路径释放占位（比较 owner，不删别人的）；失败由短 TTL 兜底。"""
        key = self._key(tenant_id, user_id, idempotency_key)
        try:
            await compare_delete(
                self._client, key, self._claim_value(request_hash, owner)
            )
        except redis_exc.RedisError:  # noqa: BLE001 - 失败由短 TTL 兜底
            logger.warning("幂等占位释放失败（TTL 兜底）")
