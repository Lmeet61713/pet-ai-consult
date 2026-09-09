"""重试工具（v5 §16.3 重试原则）

可以重试：连接中断、502/503/504、明确限时网络异常、模型输出 JSON 格式失败一次。
不能重试：401/403、参数错误、内容审核拒绝、无幂等键的副作用请求、连续显存溢出。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = {502, 503, 504}


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True
    return False


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 2,
    backoff_s: float = 0.2,
    label: str = "call",
) -> T:
    """指数退避重试；最后一次异常原样抛出。"""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_retryable(exc) or i == attempts - 1:
                break
            delay = backoff_s * (2**i)
            logger.warning("%s 重试 %d/%d: %s（%ss 后）", label, i + 1, attempts, exc, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
