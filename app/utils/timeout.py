"""外部调用超时封装（v5 §16.2 超时表）"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.exceptions import ExternalServiceTimeout

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def run_with_timeout(
    fn: Callable[[], Awaitable[T]],
    *,
    timeout_s: float,
    service: str,
) -> T:
    """超时转 ExternalServiceTimeout（上层映射为可重试错误，不输出猜测内容）。"""
    try:
        return await asyncio.wait_for(fn(), timeout=timeout_s)
    except TimeoutError:
        logger.warning("%s 调用超时（%ss）", service, timeout_s)
        raise ExternalServiceTimeout(f"{service} 暂时不可用，请稍后重试。") from None
