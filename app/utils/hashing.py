"""图片内容哈希（v5 §11.1：计算 SHA-256）"""
from __future__ import annotations

import hashlib


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
