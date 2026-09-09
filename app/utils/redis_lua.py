"""Redis Lua 原子操作（V1.1 P0-2 / P1-4）

生产 Redis 使用 Lua 原子"比较后删除/写入"（避免 A 操作 B 后来获得的新锁/占位）；
fakeredis 等不支持 eval 的测试环境退化为 get+del（存在极小竞态窗口，
仅测试/受限环境使用，生产始终走 Lua 原子路径）。
"""
from __future__ import annotations

import logging

import redis.exceptions as redis_exc

logger = logging.getLogger(__name__)

_COMPARE_DELETE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_COMPARE_SET_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    redis.call("set", KEYS[1], ARGV[2], "EX", ARGV[3])
    return 1
else
    return 0
end
"""

_INCR_WITH_TTL_LUA = """
local current = redis.call("incr", KEYS[1])
if current == 1 then
    redis.call("expire", KEYS[1], ARGV[1])
end
return current
"""


async def compare_delete(client, key: str, value: str) -> None:
    """仅当 key 当前值 == value 时删除；否则不动。"""
    try:
        await client.eval(_COMPARE_DELETE_LUA, 1, key, value)
    except redis_exc.ResponseError as exc:
        if "unknown command" not in str(exc).lower():
            raise
        # fakeredis 兜底：非原子 get+del
        current = await client.get(key)
        if current == value:
            await client.delete(key)


async def compare_set(
    client, key: str, expected: str, value: str, *, ttl_seconds: int
) -> bool:
    """仅当 key 当前值等于 expected 时写入新值并刷新 TTL。"""
    try:
        result = await client.eval(
            _COMPARE_SET_LUA, 1, key, expected, value, str(ttl_seconds)
        )
        return bool(result)
    except redis_exc.ResponseError as exc:
        if "unknown command" not in str(exc).lower():
            raise
        # fakeredis 兜底；生产 Redis 始终使用上面的 Lua 原子路径。
        current = await client.get(key)
        if current != expected:
            return False
        await client.set(key, value, ex=ttl_seconds)
        return True


async def incr_with_ttl(client, key: str, *, ttl_seconds: int) -> int:
    """Atomically increment a counter and set its TTL on the first increment."""
    try:
        result = await client.eval(_INCR_WITH_TTL_LUA, 1, key, str(ttl_seconds))
        return int(result)
    except redis_exc.ResponseError as exc:
        if "unknown command" not in str(exc).lower():
            raise
        # Test/restricted Redis fallback: preserve fixed-window semantics. Production
        # Redis uses the Lua path above; this fallback cannot make the conditional
        # EXPIRE fully atomic without script support.
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            values = await pipe.execute()
        current = int(values[0])
        if current == 1:
            await client.expire(key, ttl_seconds)
        return current
