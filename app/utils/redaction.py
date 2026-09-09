"""日志脱敏辅助（v5 §21.1：核心逻辑在 core/logging.py 的 _scrub）"""
from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://[^\s\"']+")
_ID_RE = re.compile(r"\b(?:user|u)_[a-zA-Z0-9]{4,}\b")


def redact_url(text: str) -> str:
    """移除公网 URL（图片 URL 等）"""
    return _URL_RE.sub("[url]", text)


def redact_user_id(text: str) -> str:
    return _ID_RE.sub("[uid]", text)
