"""认证上下文（v5 §22.2：tenant_id / user_id 只来自服务端验证后的 Token）"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AuthContext(BaseModel):
    tenant_id: str
    user_id: str
    token_valid: bool = True
    scope: list[str] = Field(default_factory=lambda: ["pet:consult"])
