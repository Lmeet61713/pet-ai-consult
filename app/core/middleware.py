"""请求中间件：request_id 生成、耗时、日志串联（v5 §21.3 / V1.1 P1-5）

- request_id 注入（响应头 X-Request-Id 透出）
- conversation_id 明文不落日志：path 模板化 + 独立 HMAC 摘要（LOG_HASH_SECRET）
"""
from __future__ import annotations

import logging
import re
import secrets
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import hash_id

logger = logging.getLogger(__name__)

# 会话接口路径：/api/v1/conversations/{id}
_CONV_PATH_RE = re.compile(r"/conversations/([^/]+)")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """每个请求注入 request_id（响应头 X-Request-Id 透出），记录总耗时。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id") or "req_" + secrets.token_hex(8)
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Latency-Ms"] = str(elapsed_ms)

        # V1.1 P1-5：会话 ID 脱敏——path 模板化 + HMAC 摘要，不记明文
        container = getattr(request.app.state, "container", None)
        secret = getattr(container, "settings", None) and container.settings.log_hash_secret or ""
        path = request.url.path
        extra = {
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "total_latency_ms": elapsed_ms,
        }
        m = _CONV_PATH_RE.search(path)
        if m:
            conv_id = m.group(1)
            extra["path"] = path.replace(conv_id, "{conversation_id}")
            extra["conversation_id_hash"] = hash_id(conv_id, secret)
        logger.info("request_done", extra=extra)
        return response
