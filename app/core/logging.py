"""结构化日志：JSON 输出 + 敏感字段脱敏（v5 §21.1）

不记录：原始图片、base64、API Key、用户完整隐私文本、未脱敏的用户 ID、
DeepSeek 完整原始请求。
"""
from __future__ import annotations

import json
import logging
import re

# 已知的敏感 key（递归脱敏）
_SENSITIVE_KEYS = frozenset(
    {"api_key", "apikey", "token", "secret", "password", "authorization", "cookie", "base64", "key"}
)

_ID_HASH_RE = re.compile(r"[a-zA-Z0-9_\-]{8,}")


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in _SENSITIVE_KEYS else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(x) for x in obj]
    return obj


class JsonFormatter(logging.Formatter):
    """输出单行 JSON：{ts, level, logger, msg, ...extra}"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in ("message", "asctime", "msg", "args", "exc_info", "exc_text", "stack_info", "levelname", "name", "levelno", "pathname", "filename", "module", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread", "threadName", "processName", "process", "taskName"):
                continue
            payload[key] = _scrub(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def hash_id(value: str, secret: str = "") -> str:
    """用户 ID / 会话 ID 脱敏（V1.1 P1-5）。

    - 配置 LOG_HASH_SECRET 时用 HMAC-SHA256（防字典攻击、支持密钥轮换）
    - 未配置时退化 sha256（本地开发；生产启动校验要求配置）
    """
    import hashlib
    import hmac

    if secret:
        return hmac.new(
            secret.encode(), value.encode(), hashlib.sha256
        ).hexdigest()[:16]
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    # 第三方库降噪
    for noisy in ("uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
